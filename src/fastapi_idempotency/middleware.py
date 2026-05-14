"""Starlette/FastAPI ASGI middleware entrypoint."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import TYPE_CHECKING, ClassVar

from .body_buffer import buffer_request_body
from .errors import RequestTooLargeError, StoreError
from .fingerprint import compute_fingerprint
from .types import (
    AcquireOutcome,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from .store import Store


logger = logging.getLogger(__name__)


class _Missing:
    """Sentinel type for the ``secret`` kwarg default.

    Forces callers to choose ``secret=os.environ[...].encode()`` (HMAC)
    or ``secret=None`` (explicit insecure mode) at construction. Typed
    as its own class so ``mypy --strict`` narrows correctly inside the
    ``isinstance`` guard rather than falling back to ``Any``.
    """

    def __repr__(self) -> str:
        return "<unset>"


_SECRET_NOT_SET = _Missing()


# Stripped from the cached response — see ``docs/DESIGN.md``
# ("Volatile response headers stripped before caching") for the
# threat model and the identity / connection-level split.
_VOLATILE_HEADER_DENYLIST: frozenset[bytes] = frozenset(
    {
        # Identity / credential headers
        b"set-cookie",
        b"authorization",
        b"proxy-authorization",
        b"www-authenticate",
        b"proxy-authenticate",
        b"cookie",
        # Hop-by-hop (RFC 7230 §6.1, RFC 9110 §7.6.1)
        b"connection",
        b"keep-alive",
        b"transfer-encoding",
        b"upgrade",
        b"trailer",
    },
)


def _strip_volatile_headers(
    headers: tuple[tuple[bytes, bytes], ...],
) -> tuple[tuple[bytes, bytes], ...]:
    return tuple(
        (name, value) for name, value in headers if name.lower() not in _VOLATILE_HEADER_DENYLIST
    )


_LOG_HASH_HEX_LEN = 12


def _hash_for_log(value: bytes, secret: bytes | None) -> str:
    h = hmac.new(secret, value, hashlib.sha256) if secret is not None else hashlib.sha256(value)
    return h.hexdigest()[:_LOG_HASH_HEX_LEN]


def _log_context(
    key: IdempotencyKey,
    secret: bytes | None,
    *,
    fingerprint: Fingerprint | None = None,
    outcome: AcquireOutcome | None = None,
    status: int | None = None,
) -> dict[str, str]:
    """Structured ``extra=`` payload — keeps raw keys out of logs."""
    ctx: dict[str, str] = {"key_hash": _hash_for_log(key.encode(), secret)}
    if fingerprint is not None:
        ctx["fingerprint"] = fingerprint[:_LOG_HASH_HEX_LEN]
    if outcome is not None:
        ctx["outcome"] = outcome.value
    if status is not None:
        ctx["status"] = str(status)
    return ctx


class IdempotencyMiddleware:
    """ASGI middleware enforcing ``Idempotency-Key`` semantics.

    Only non-safe methods (POST, PATCH, PUT, DELETE) are intercepted; safe
    requests pass through untouched. Built on raw ASGI rather than
    ``BaseHTTPMiddleware`` so streaming responses can pass through live.
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
        secret: bytes | None | _Missing = _SECRET_NOT_SET,
        header_name: str = "Idempotency-Key",
        in_flight_ttl: float = 30.0,
        completed_ttl: float = 86_400.0,
        max_body_bytes: int | None = None,
        require_key: bool = False,
    ) -> None:
        if isinstance(secret, _Missing):
            msg = (
                "secret= is required. Pass "
                "`secret=os.environ['IDEMP_SECRET'].encode()` for HMAC-SHA256 "
                "fingerprints. See README for the opt-out path used in tests."
            )
            raise ValueError(msg)
        self.app = app
        self.store = store
        self._secret: bytes | None = secret
        self.header_name = header_name
        self.in_flight_ttl = in_flight_ttl
        self.completed_ttl = completed_ttl
        self.max_body_bytes = max_body_bytes
        self.require_key = require_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] not in self.NON_SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        key_value = self._extract_key(scope)
        if key_value is None:
            if self.require_key:
                method = scope["method"]
                await self._send_plain_response(
                    send,
                    status=400,
                    message=f"Idempotency-Key header required for {method} requests",
                )
                return
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
        try:
            body, replay = await buffer_request_body(
                receive,
                max_bytes=self.max_body_bytes,
            )
        except RequestTooLargeError:
            # Fixed message: never echo exception text or limit value to
            # the client (defends against future leak via exception state).
            # The remaining unread http.request messages are the ASGI
            # server's responsibility — draining here would defeat
            # early rejection of attack-sized bodies.
            await self._send_plain_response(
                send,
                status=413,
                message="request body exceeds maximum allowed size",
            )
            return
        fingerprint = compute_fingerprint(
            scope["method"],
            scope["path"],
            scope.get("query_string", b""),
            body,
            secret=self._secret,
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
                # Store-contract violation: REPLAY without a response.
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
            logger.info(
                "concurrent request for in-flight Idempotency-Key; responding 409",
                extra=_log_context(
                    key,
                    self._secret,
                    fingerprint=fingerprint,
                    outcome=result.outcome,
                    status=409,
                ),
            )
            await self._send_plain_response(
                send,
                status=409,
                message="request with this Idempotency-Key is in progress",
            )
            return

        # MISMATCH — log at WARNING: a key reused with a different body
        # is either a buggy client or a probe. See DESIGN.md
        # ("Logging — keys hashed, hot path silent").
        logger.warning(
            "Idempotency-Key reused with a different body; responding 422",
            extra=_log_context(
                key,
                self._secret,
                fingerprint=fingerprint,
                outcome=result.outcome,
                status=422,
            ),
        )
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
        interceptor = _ResponseInterceptor(send)
        try:
            await self.app(scope, replay, interceptor)
        except Exception as exc:
            # No ``exc_info=True``: downstream exceptions may carry the
            # raw key in their message, and the traceback would render
            # it unredacted. See DESIGN.md ("Logging — keys hashed").
            logger.warning(
                "exception inside intercepted handler; releasing slot",
                extra={
                    **_log_context(key, self._secret),
                    "exc_type": type(exc).__name__,
                },
            )
            await self.store.release(key)
            raise

        if interceptor.streamed:
            # Streams can't be cached — see ``CachedResponse`` invariant.
            await self.store.release(key)
            return

        if interceptor.status is None:
            # Handler returned without emitting http.response.start —
            # synthesize a 500 so the wire contract holds.
            logger.warning(
                "handler returned without sending http.response.start; emitting 500",
                extra=_log_context(key, self._secret, status=500),
            )
            await self.store.release(key)
            await self._send_plain_response(
                send,
                status=500,
                message="upstream handler emitted no response",
                extra_headers=[(b"idempotency-stored", b"false")],
            )
            return

        if interceptor.status >= self.FIRST_SERVER_ERROR_STATUS:
            # 5xx is transient — drop the slot so a retry runs fresh.
            await self.store.release(key)
            await self._send_response(
                send,
                status=interceptor.status,
                headers=list(interceptor.headers),
                body=bytes(interceptor.body),
            )
            return

        # Strip for the cached copy only; the first caller below
        # receives ``interceptor.headers`` untouched.
        cached = CachedResponse(
            status_code=interceptor.status,
            headers=_strip_volatile_headers(interceptor.headers),
            body=bytes(interceptor.body),
        )
        extra_headers: list[tuple[bytes, bytes]] = []
        try:
            await self.store.complete(key, cached, ttl=self.completed_ttl)
        except StoreError:
            logger.warning(
                "store.complete raised StoreError; serving uncached "
                "response with Idempotency-Stored: false",
                extra=_log_context(key, self._secret),
            )
            extra_headers = [(b"idempotency-stored", b"false")]

        await self._send_response(
            send,
            status=interceptor.status,
            headers=[*interceptor.headers, *extra_headers],
            body=bytes(interceptor.body),
        )

    def _extract_key(self, scope: Scope) -> str | None:
        raw = self._get_header(scope, self.header_name.encode("latin-1"))
        return raw.decode("latin-1") if raw is not None else None

    def _is_valid_key(self, value: str) -> bool:
        # Per IETF draft: ASCII, 1..MAX_KEY_LENGTH chars.
        return 1 <= len(value) <= self.MAX_KEY_LENGTH and value.isascii()

    def _content_length_exceeds_limit(self, scope: Scope) -> bool:
        # Pre-check: we can't recover mid-buffer, so Content-Length is
        # the only fence we trust before letting the request through.
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
        """Return the first header matching ``name`` (case-insensitive)."""
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
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        """Send a plain-text response with the given status and message body."""
        body = message.encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        await self._send_response(
            send,
            status=status,
            headers=headers,
            body=body,
        )


class _ResponseInterceptor:
    """Buffers a non-streaming response or forwards a streaming one live.

    See ``docs/DESIGN.md`` ("Streaming response pass-through") for the
    deferred-start mechanism and the rationale for reusing
    ``Idempotency-Stored: false`` on streamed responses.
    """

    def __init__(self, send: Send) -> None:
        self._send = send
        self.status: int | None = None
        self.headers: tuple[tuple[bytes, bytes], ...] = ()
        self.body = bytearray()
        self.streamed = False
        self._pending_start: Message | None = None

    async def __call__(self, message: Message) -> None:
        if self.streamed:
            await self._send(message)
            return

        if message["type"] == "http.response.start":
            if self._pending_start is not None:
                msg = "ASGI protocol violation: duplicate http.response.start"
                raise RuntimeError(msg)
            self._pending_start = message
            self.status = message["status"]
            self.headers = tuple(message.get("headers", []))
            return

        if message["type"] == "http.response.body":
            more_body = message.get("more_body", False)
            if more_body:
                if self._pending_start is None:
                    # Typed raise (not assert) so it survives ``python -O``.
                    msg = "ASGI protocol violation: http.response.body before http.response.start"
                    raise RuntimeError(msg)
                patched_start: Message = dict(self._pending_start)
                patched_headers = list(patched_start.get("headers", []))
                patched_headers.append((b"idempotency-stored", b"false"))
                patched_start["headers"] = patched_headers

                self.streamed = True
                self._pending_start = None
                await self._send(patched_start)
                await self._send(message)
                return

            # Clear so a second http.response.start re-trips the guard.
            self._pending_start = None
            self.body.extend(message.get("body", b""))
