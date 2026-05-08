"""Acceptance tests for IdempotencyMiddleware.

Tests use httpx.ASGITransport for HTTP cases and direct ASGI calls for
edge cases like non-HTTP scope types.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import httpx

from fastapi_idempotency import (
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyMiddleware,
    IdempotencyState,
    InMemoryStore,
    StoreError,
)
from fastapi_idempotency.types import AcquireResult, IdempotencyRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import Message, Receive, Scope, Send


async def echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Minimal ASGI app: returns the request body, or 'ok' if empty."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break

    body = b"".join(chunks) or b"ok"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        },
    )
    await send({"type": "http.response.body", "body": body})


def make_client(app: Any) -> httpx.AsyncClient:
    middleware = IdempotencyMiddleware(app, InMemoryStore())
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    )


async def test_get_passes_through_to_handler() -> None:
    async with make_client(echo_app) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.text == "ok"


async def test_options_passes_through_to_handler() -> None:
    async with make_client(echo_app) as client:
        response = await client.options("/")

    assert response.status_code == 200


async def test_post_without_key_passes_through_to_handler() -> None:
    async with make_client(echo_app) as client:
        response = await client.post("/", content=b"hello")

    assert response.status_code == 200
    assert response.content == b"hello"


async def test_non_http_scope_passes_through() -> None:
    """Lifespan / websocket scopes should reach the wrapped app untouched."""
    seen_scopes: list[str] = []

    async def app(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen_scopes.append(scope["type"])

    middleware = IdempotencyMiddleware(app, InMemoryStore())

    async def _noop_receive() -> Message:
        return {"type": "lifespan.startup"}

    async def _noop_send(_message: Message) -> None:
        return None

    scope: Scope = {"type": "lifespan"}
    await middleware(scope, _noop_receive, _noop_send)

    assert seen_scopes == ["lifespan"]


async def test_too_long_key_returns_400() -> None:
    long_key = "a" * 256
    async with make_client(echo_app) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": long_key},
            content=b"x",
        )

    assert response.status_code == 400
    assert response.text == "invalid Idempotency-Key"


async def test_non_ascii_key_returns_400() -> None:
    # Bypass httpx's str-to-ASCII encoding by sending raw bytes.
    headers = httpx.Headers([(b"Idempotency-Key", b"\xf1")])
    async with make_client(echo_app) as client:
        response = await client.post("/", headers=headers, content=b"x")

    assert response.status_code == 400


async def test_empty_key_returns_400() -> None:
    """Empty header value fails the 1-255 length rule."""
    async with make_client(echo_app) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": ""},
            content=b"x",
        )

    assert response.status_code == 400


async def test_first_post_runs_handler_and_returns_response() -> None:
    async with make_client(echo_app) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "abc"},
            content=b"hello",
        )

    assert response.status_code == 200
    assert response.content == b"hello"


async def test_first_post_stores_completed_record() -> None:
    store = InMemoryStore()
    middleware = IdempotencyMiddleware(echo_app, store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        await client.post(
            "/",
            headers={"Idempotency-Key": "abc"},
            content=b"hello",
        )

    record = await store.get(IdempotencyKey("abc"))
    assert record is not None
    assert record.state is IdempotencyState.COMPLETED
    assert record.response is not None
    assert record.response.status_code == 200
    assert record.response.body == b"hello"


async def test_replay_returns_cached_response_with_replayed_header() -> None:
    async with make_client(echo_app) as client:
        first = await client.post(
            "/",
            headers={"Idempotency-Key": "abc"},
            content=b"hello",
        )
        second = await client.post(
            "/",
            headers={"Idempotency-Key": "abc"},
            content=b"hello",
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.content == b"hello"
    assert second.headers["idempotent-replayed"] == "true"


async def test_same_key_different_body_returns_422() -> None:
    async with make_client(echo_app) as client:
        first = await client.post(
            "/",
            headers={"Idempotency-Key": "abc"},
            content=b"hello",
        )
        second = await client.post(
            "/",
            headers={"Idempotency-Key": "abc"},
            content=b"world",
        )

    assert first.status_code == 200
    assert second.status_code == 422


async def test_5xx_response_releases_slot() -> None:
    """Server errors aren't cached — the slot is dropped so a retry runs fresh."""

    async def server_error_app(
        _scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            if not message.get("more_body", False):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"text/plain")],
            },
        )
        await send({"type": "http.response.body", "body": b"oops"})

    store = InMemoryStore()
    middleware = IdempotencyMiddleware(server_error_app, store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=b"a",
        )

    assert response.status_code == 500
    assert await store.get(IdempotencyKey("x")) is None


async def test_5xx_then_retry_runs_handler_again() -> None:
    """After 5xx, the slot is gone — a retry hits the handler, not REPLAY."""
    invocations = 0

    async def flaky_app(
        _scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        nonlocal invocations
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            if not message.get("more_body", False):
                break
        invocations += 1
        status = 500 if invocations == 1 else 200
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain")],
            },
        )
        await send(
            {"type": "http.response.body", "body": str(invocations).encode()},
        )

    middleware = IdempotencyMiddleware(flaky_app, InMemoryStore())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        first = await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=b"a",
        )
        second = await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=b"a",
        )

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.text == "2"
    assert invocations == 2


async def test_handler_exception_releases_slot() -> None:
    """If the wrapped app raises before sending a response, release the slot."""

    async def boom_app(
        _scope: Scope,
        receive: Receive,
        _send: Send,
    ) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            if not message.get("more_body", False):
                break
        msg = "boom"
        raise RuntimeError(msg)

    store = InMemoryStore()
    middleware = IdempotencyMiddleware(boom_app, store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        with contextlib.suppress(Exception):
            await client.post(
                "/",
                headers={"Idempotency-Key": "x"},
                content=b"a",
            )

    assert await store.get(IdempotencyKey("x")) is None


async def test_oversized_body_passes_through_without_idempotency() -> None:
    """Content-Length over max_body_bytes → handler runs, no slot recorded."""
    store = InMemoryStore()
    middleware = IdempotencyMiddleware(echo_app, store, max_body_bytes=100)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=b"a" * 200,
        )

    assert response.status_code == 200
    assert response.content == b"a" * 200
    # Nothing was recorded — idempotency was skipped.
    assert await store.get(IdempotencyKey("x")) is None


async def test_chunked_body_overflow_returns_413() -> None:
    """Chunked-transfer body (no Content-Length) > max_body_bytes:
    middleware catches ``RequestTooLargeError`` from ``buffer_request_body``
    and responds 413 with a fixed message — no handler invocation, no
    idempotency record."""
    invocations = 0

    async def counting_app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal invocations
        invocations += 1
        # Drain anyway so the test is honest about whether we got here.
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            if not message.get("more_body", False):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            },
        )
        await send({"type": "http.response.body", "body": b"ok"})

    store = InMemoryStore()
    middleware = IdempotencyMiddleware(counting_app, store, max_body_bytes=100)

    async def chunks() -> AsyncIterator[bytes]:
        # Two 60-byte chunks → 120 bytes total > max_body_bytes=100.
        # httpx omits Content-Length when content is an async iterator,
        # so the Content-Length pre-check can't pass-through; the
        # in-buffer fence is the only defense.
        yield b"a" * 60
        yield b"b" * 60

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=chunks(),
        )

    assert response.status_code == 413
    assert response.text == "request body exceeds maximum allowed size"
    assert invocations == 0
    assert await store.get(IdempotencyKey("x")) is None


async def test_single_oversized_chunk_returns_413() -> None:
    """One chunk over the limit (no Content-Length): proves the in-buffer
    fence checks per chunk, not just on the second."""
    invocations = 0

    async def counting_app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal invocations
        invocations += 1
        await echo_app(scope, receive, send)

    store = InMemoryStore()
    middleware = IdempotencyMiddleware(counting_app, store, max_body_bytes=100)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"a" * 200

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=chunks(),
        )

    assert response.status_code == 413
    assert response.text == "request body exceeds maximum allowed size"
    assert invocations == 0


async def test_chunked_body_at_exact_limit_passes() -> None:
    """Boundary: ``total > max_bytes`` (strict greater-than) — a body of
    exactly ``max_body_bytes`` must succeed."""
    store = InMemoryStore()
    middleware = IdempotencyMiddleware(echo_app, store, max_body_bytes=100)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"a" * 100

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=chunks(),
        )

    assert response.status_code == 200
    assert response.content == b"a" * 100


async def test_undersized_body_uses_idempotency() -> None:
    """Sanity: when Content-Length is within the limit, the slot is recorded."""
    store = InMemoryStore()
    middleware = IdempotencyMiddleware(echo_app, store, max_body_bytes=100)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=b"a" * 50,
        )

    assert await store.get(IdempotencyKey("x")) is not None


async def test_store_error_on_complete_adds_idempotency_stored_false_header() -> None:
    """If store.complete raises, the response is delivered with a header
    flagging that retries won't replay."""

    class FailingCompleteStore:
        """Wraps an InMemoryStore but always raises on complete."""

        def __init__(self) -> None:
            self._inner = InMemoryStore()

        async def acquire(
            self,
            key: IdempotencyKey,
            fingerprint: Fingerprint,
            ttl: float,
        ) -> AcquireResult:
            return await self._inner.acquire(key, fingerprint, ttl)

        async def get(
            self,
            key: IdempotencyKey,
        ) -> IdempotencyRecord | None:
            return await self._inner.get(key)

        async def complete(
            self,
            key: IdempotencyKey,
            response: CachedResponse,
            ttl: float,
        ) -> None:
            del key, response, ttl
            msg = "simulated eviction"
            raise StoreError(msg)

        async def release(self, key: IdempotencyKey) -> None:
            await self._inner.release(key)

    middleware = IdempotencyMiddleware(echo_app, FailingCompleteStore())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "x"},
            content=b"hello",
        )

    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["idempotency-stored"] == "false"


async def test_in_flight_ttl_expiry_reclaims_orphaned_slot() -> None:
    """Crash-orphan recovery: a handler that runs longer than in_flight_ttl
    loses its slot, so the next request gets a fresh CREATED instead of
    being blocked by IN_FLIGHT or MISMATCH."""
    invocations = 0

    async def slow_handler(
        _scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        nonlocal invocations
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            if not message.get("more_body", False):
                break
        invocations += 1
        await asyncio.sleep(0.1)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            },
        )
        await send(
            {"type": "http.response.body", "body": str(invocations).encode()},
        )

    middleware = IdempotencyMiddleware(
        slow_handler,
        InMemoryStore(),
        in_flight_ttl=0.02,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:

        async def first() -> httpx.Response:
            return await client.post(
                "/",
                headers={"Idempotency-Key": "x"},
                content=b"a",
            )

        async def second_after_ttl() -> httpx.Response:
            await asyncio.sleep(0.05)  # > in_flight_ttl
            return await client.post(
                "/",
                headers={"Idempotency-Key": "x"},
                content=b"a",
            )

        r1, r2 = await asyncio.gather(first(), second_after_ttl())

    # Both ran the handler — second wasn't blocked by the still-running first.
    assert invocations == 2
    assert r1.status_code == 200
    assert r2.status_code == 200


async def test_concurrent_request_with_same_key_returns_409() -> None:
    in_handler = asyncio.Event()
    can_finish = asyncio.Event()

    async def slow_app(scope: Scope, receive: Receive, send: Send) -> None:
        # Drain the request body so the middleware can buffer-and-replay.
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            if not message.get("more_body", False):
                break

        in_handler.set()
        await can_finish.wait()

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            },
        )
        await send({"type": "http.response.body", "body": b"slow"})

    async with make_client(slow_app) as client:

        async def first() -> httpx.Response:
            return await client.post(
                "/",
                headers={"Idempotency-Key": "x"},
                content=b"a",
            )

        async def second() -> httpx.Response:
            await in_handler.wait()
            try:
                return await client.post(
                    "/",
                    headers={"Idempotency-Key": "x"},
                    content=b"a",
                )
            finally:
                can_finish.set()

        r1, r2 = await asyncio.gather(first(), second())

    assert r1.status_code == 200
    assert r2.status_code == 409
