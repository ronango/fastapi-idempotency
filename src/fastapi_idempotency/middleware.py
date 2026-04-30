"""Starlette/FastAPI ASGI middleware entrypoint."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from .body_buffer import buffer_request_body
from .errors import StoreError
from .fingerprint import compute_fingerprint
from .types import (
    AcquireOutcome,
    CachedResponse,
    IdempotencyKey,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from .store import Store


logger = logging.getLogger(__name__)


class IdempotencyMiddleware:
    """ASGI middleware enforcing ``Idempotency-Key`` semantics.

    Only non-safe methods (POST, PATCH, PUT, DELETE) are intercepted; safe
    requests pass through untouched. Implemented as a raw ASGI class rather
    than :class:`starlette.middleware.base.BaseHTTPMiddleware` so streaming
    response pass-through (v0.2.0) remains possible.
    """

    NON_SAFE_METHODS: ClassVar[frozenset[str]] = frozenset(
        {"POST", "PATCH", "PUT", "DELETE"},
    )
    MAX_KEY_LENGTH: ClassVar[int] = 255
    FIRST_SERVER_ERROR_STATUS: ClassVar[int] = 500

    def __init__(
        self,
        app: ASGIApp,
        store: Store,
        *,
        header_name: str = "Idempotency-Key",
        in_flight_ttl: float = 30.0,
        completed_ttl: float = 86_400.0,
        max_body_bytes: int | None = None,
    ) -> None:
        self.app = app
        self.store = store
        self.header_name = header_name
        self.in_flight_ttl = in_flight_ttl
        self.completed_ttl = completed_ttl
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] not in self.NON_SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        key_value = self._extract_key(scope)
        if key_value is None:
            await self.app(scope, receive, send)
            return

        if not self._is_valid_key(key_value):
            await self._send_plain_response(
                send,
                status=400,
                message="invalid Idempotency-Key",
            )
            return

        if self._content_length_exceeds_limit(scope):
            await self.app(scope, receive, send)
            return

        await self._handle_intercepted(
            scope,
            receive,
            send,
            IdempotencyKey(key_value),
        )

    async def _handle_intercepted(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        key: IdempotencyKey,
    ) -> None:
        body, replay = await buffer_request_body(
            receive,
            max_bytes=self.max_body_bytes,
        )
        fingerprint = compute_fingerprint(
            scope["method"],
            scope["path"],
            scope.get("query_string", b""),
            body,
        )

        result = await self.store.acquire(
            key,
            fingerprint,
            ttl=self.in_flight_ttl,
        )

        if result.outcome is AcquireOutcome.CREATED:
            await self._run_and_cache(scope, replay, send, key)
            return

        if result.outcome is AcquireOutcome.REPLAY:
            cached = result.record.response
            if cached is None:
                # Store contract says REPLAY → response is set; if we ever
                # see this, it's a store bug. Treat as 500.
                await self._send_plain_response(
                    send,
                    status=500,
                    message="cached response missing",
                )
                return
            await self._send_response(
                send,
                status=cached.status_code,
                headers=[*cached.headers, (b"idempotent-replayed", b"true")],
                body=cached.body,
            )
            return

        if result.outcome is AcquireOutcome.IN_FLIGHT:
            await self._send_plain_response(
                send,
                status=409,
                message="request with this Idempotency-Key is in progress",
            )
            return

        # MISMATCH
        await self._send_plain_response(
            send,
            status=422,
            message="Idempotency-Key reused with a different request body",
        )

    async def _run_and_cache(
        self,
        scope: Scope,
        replay: Receive,
        send: Send,
        key: IdempotencyKey,
    ) -> None:
        capturer = _ResponseCapturer()
        try:
            await self.app(scope, replay, capturer)
        except Exception:
            # Handler raised before producing a response. Release the slot
            # so a retry can run fresh, then propagate.
            await self.store.release(key)
            raise

        if capturer.status is None or capturer.status >= self.FIRST_SERVER_ERROR_STATUS:
            # 5xx (or no response at all) is treated as a transient failure.
            # Don't cache it — drop the slot so a retry runs the handler again.
            await self.store.release(key)
            await self._send_response(
                send,
                status=capturer.status or 0,
                headers=list(capturer.headers),
                body=bytes(capturer.body),
            )
            return

        cached = CachedResponse(
            status_code=capturer.status,
            headers=capturer.headers,
            body=bytes(capturer.body),
        )
        extra_headers: list[tuple[bytes, bytes]] = []
        try:
            await self.store.complete(key, cached, ttl=self.completed_ttl)
        except StoreError:
            # Per the deferred decision in issue #3: the request itself
            # succeeded; surface the storage failure to the client via a
            # header so retries know they won't replay.
            logger.warning(
                "store.complete raised StoreError for key=%r; serving "
                "uncached response with Idempotency-Stored: false",
                key,
            )
            extra_headers = [(b"idempotency-stored", b"false")]

        await self._send_response(
            send,
            status=capturer.status,
            headers=[*capturer.headers, *extra_headers],
            body=bytes(capturer.body),
        )

    def _extract_key(self, scope: Scope) -> str | None:
        raw = self._get_header(scope, self.header_name.encode("latin-1"))
        return raw.decode("latin-1") if raw is not None else None

    def _is_valid_key(self, value: str) -> bool:
        # Per IETF draft: ASCII, 1..MAX_KEY_LENGTH chars.
        return 1 <= len(value) <= self.MAX_KEY_LENGTH and value.isascii()

    def _content_length_exceeds_limit(self, scope: Scope) -> bool:
        # Pre-check: if Content-Length declares a body bigger than the
        # configured limit, skip idempotency entirely. We can't recover
        # mid-buffer, so this header is the only fence we trust.
        if self.max_body_bytes is None:
            return False
        raw = self._get_header(scope, b"content-length")
        if raw is None:
            return False
        try:
            declared = int(raw)
        except ValueError:
            return False
        return declared > self.max_body_bytes

    @staticmethod
    def _get_header(scope: Scope, name: bytes) -> bytes | None:
        """Return the first header matching ``name`` (case-insensitive).

        ASGI delivers headers as a list of ``(bytes, bytes)`` tuples
        with names lowercased by the server, but lower-casing both sides
        keeps the helper safe regardless of the caller's input.
        """
        target = name.lower()
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for header_name, header_value in headers:
            if header_name.lower() == target:
                return header_value
        return None

    @staticmethod
    async def _send_response(
        send: Send,
        *,
        status: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
    ) -> None:
        """Emit a complete ASGI response (start + body, no streaming)."""
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            },
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            },
        )

    async def _send_plain_response(
        self,
        send: Send,
        *,
        status: int,
        message: str,
    ) -> None:
        """Send a plain-text response with the given status and message body."""
        body = message.encode("utf-8")
        await self._send_response(
            send,
            status=status,
            headers=[
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            body=body,
        )


class _ResponseCapturer:
    """Buffers status, headers, and body emitted by the wrapped app.

    The middleware emits the captured response to its own ``send`` after
    it has decided whether to ``complete`` or ``release`` the slot —
    that lets it amend headers (e.g., ``Idempotency-Stored: false``)
    based on the storage outcome.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: tuple[tuple[bytes, bytes], ...] = ()
        self.body = bytearray()

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = tuple(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.body.extend(message.get("body", b""))
