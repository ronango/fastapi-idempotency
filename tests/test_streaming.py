"""Streaming response pass-through.

Unit tests probe ``_ResponseInterceptor`` with a recorder ``send``;
integration tests drive ``IdempotencyMiddleware`` end-to-end via
httpx ASGITransport. See ``docs/DESIGN.md`` for the design rationale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from fastapi_idempotency import (
    IdempotencyKey,
    IdempotencyMiddleware,
    InMemoryStore,
)
from fastapi_idempotency.middleware import _ResponseInterceptor

if TYPE_CHECKING:
    from starlette.types import Message, Receive, Scope, Send


# Unit: ``_ResponseInterceptor``  ----------------------------------------------


class _SendRecorder:
    """Mock ASGI send: appends every message to ``messages`` for assertions."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)


async def test_interceptor_buffers_single_chunk_response_without_forwarding() -> None:
    """Non-streaming: nothing forwarded; status/headers/body buffered."""
    recorder = _SendRecorder()
    interceptor = _ResponseInterceptor(recorder)

    await interceptor({"type": "http.response.start", "status": 200, "headers": [(b"x", b"y")]})
    await interceptor({"type": "http.response.body", "body": b"hello", "more_body": False})

    assert recorder.messages == []  # buffered, not forwarded
    assert interceptor.streamed is False
    assert interceptor.status == 200
    assert interceptor.headers == ((b"x", b"y"),)
    assert bytes(interceptor.body) == b"hello"


async def test_interceptor_treats_missing_more_body_as_non_streaming() -> None:
    """ASGI default: ``more_body`` absent ⇒ ``False``. Stay in buffered mode."""
    recorder = _SendRecorder()
    interceptor = _ResponseInterceptor(recorder)

    await interceptor({"type": "http.response.start", "status": 200, "headers": []})
    await interceptor({"type": "http.response.body", "body": b"x"})  # no more_body key

    assert recorder.messages == []
    assert interceptor.streamed is False
    assert bytes(interceptor.body) == b"x"


async def test_interceptor_forwards_streaming_response_with_idempotency_stored_header() -> None:
    """First chunk has ``more_body=True`` → flip to forwarding mode.

    Patched ``http.response.start`` carries ``Idempotency-Stored: false``;
    every body chunk is forwarded live (none buffered into ``self.body``).
    """
    recorder = _SendRecorder()
    interceptor = _ResponseInterceptor(recorder)

    await interceptor(
        {"type": "http.response.start", "status": 200, "headers": [(b"x-trace", b"t1")]},
    )
    await interceptor({"type": "http.response.body", "body": b"chunk1", "more_body": True})
    await interceptor({"type": "http.response.body", "body": b"chunk2", "more_body": True})
    await interceptor({"type": "http.response.body", "body": b"chunk3", "more_body": False})

    assert interceptor.streamed is True
    # Memory invariant: streaming must not buffer body bytes.
    assert len(interceptor.body) == 0

    # Recorder saw 4 messages: patched start, then 3 chunks verbatim.
    assert len(recorder.messages) == 4
    start = recorder.messages[0]
    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    assert (b"idempotency-stored", b"false") in start["headers"]
    # Original headers preserved alongside the patch.
    assert (b"x-trace", b"t1") in start["headers"]
    # Body chunks forwarded unmodified.
    assert recorder.messages[1] == {
        "type": "http.response.body",
        "body": b"chunk1",
        "more_body": True,
    }
    assert recorder.messages[2]["body"] == b"chunk2"
    assert recorder.messages[3]["body"] == b"chunk3"
    assert recorder.messages[3]["more_body"] is False


async def test_interceptor_raises_on_body_before_start() -> None:
    """ASGI protocol violation: body without prior start is ``RuntimeError``,
    not ``AssertionError`` — production-grade so ``python -O`` still raises."""
    recorder = _SendRecorder()
    interceptor = _ResponseInterceptor(recorder)

    with pytest.raises(RuntimeError, match=r"http\.response\.body before"):
        await interceptor({"type": "http.response.body", "body": b"x", "more_body": True})


async def test_interceptor_raises_on_duplicate_start() -> None:
    """ASGI protocol violation: two ``http.response.start`` messages — raise
    rather than silently overwrite the captured status/headers."""
    recorder = _SendRecorder()
    interceptor = _ResponseInterceptor(recorder)

    await interceptor({"type": "http.response.start", "status": 200, "headers": []})
    with pytest.raises(RuntimeError, match=r"duplicate http\.response\.start"):
        await interceptor({"type": "http.response.start", "status": 200, "headers": []})


async def test_interceptor_does_not_mutate_original_start_message() -> None:
    """We patch via ``dict(...)`` copy; the caller's start message stays intact.

    Defends against a future maintainer dropping the copy and accidentally
    leaking the ``Idempotency-Stored`` header into the app's own state.
    """
    recorder = _SendRecorder()
    interceptor = _ResponseInterceptor(recorder)
    original_start: Message = {
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"a", b"b")],
    }

    await interceptor(original_start)
    await interceptor({"type": "http.response.body", "body": b"x", "more_body": True})

    assert original_start["headers"] == [(b"a", b"b")]


# Integration: streaming pass-through end-to-end  ---------------------------


async def _drive_request_lifecycle(receive: Receive) -> None:
    """Drain ``http.request`` messages until the body is fully read."""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        if not message.get("more_body", False):
            break


def _make_streaming_app(chunks: list[bytes]) -> Any:
    """ASGI app that emits ``chunks`` as a streaming response.

    Each chunk goes out as its own ``http.response.body`` message;
    every chunk except the last has ``more_body=True``. Mirrors what
    Starlette's ``StreamingResponse`` does on the wire.
    """

    async def app(_scope: Scope, receive: Receive, send: Send) -> None:
        await _drive_request_lifecycle(receive)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            },
        )
        for i, chunk in enumerate(chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": i < len(chunks) - 1,
                },
            )

    return app


async def test_streaming_response_flows_through_with_stored_false_header() -> None:
    chunks = [b"first ", b"second ", b"third"]
    middleware = IdempotencyMiddleware(_make_streaming_app(chunks), InMemoryStore())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "stream-1"},
            content=b"req",
        )

    assert response.status_code == 200
    assert response.content == b"first second third"
    assert response.headers.get("idempotency-stored") == "false"


async def test_streaming_response_releases_slot_so_retry_runs_handler_again() -> None:
    """A stream can't be replayed — the second request must run the handler."""
    invocations = 0

    async def streaming_app(_scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal invocations
        await _drive_request_lifecycle(receive)
        invocations += 1
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            },
        )
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await send({"type": "http.response.body", "body": b"b", "more_body": False})

    store = InMemoryStore()
    middleware = IdempotencyMiddleware(streaming_app, store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        first = await client.post(
            "/",
            headers={"Idempotency-Key": "k"},
            content=b"x",
        )
        second = await client.post(
            "/",
            headers={"Idempotency-Key": "k"},
            content=b"x",
        )

    assert first.status_code == 200
    assert first.headers.get("idempotency-stored") == "false"
    assert second.status_code == 200
    assert second.headers.get("idempotent-replayed") is None  # not a replay
    assert second.headers.get("idempotency-stored") == "false"  # also a stream
    assert invocations == 2
    assert await store.get(IdempotencyKey("k")) is None


async def test_non_streaming_response_still_caches_and_replays() -> None:
    """Single-chunk responses keep the v0.1.0 contract: caching + replay header."""
    invocations = 0

    async def single_chunk_app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal invocations
        await _drive_request_lifecycle(receive)
        invocations += 1
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"content-type", b"application/json")],
            },
        )
        await send(
            {"type": "http.response.body", "body": b'{"ok":true}', "more_body": False},
        )

    middleware = IdempotencyMiddleware(single_chunk_app, InMemoryStore())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        first = await client.post(
            "/",
            headers={"Idempotency-Key": "k2"},
            content=b"x",
        )
        second = await client.post(
            "/",
            headers={"Idempotency-Key": "k2"},
            content=b"x",
        )

    assert first.status_code == 201
    assert first.headers.get("idempotency-stored") is None
    assert first.headers.get("idempotent-replayed") is None
    assert second.status_code == 201
    assert second.content == first.content
    assert second.headers.get("idempotent-replayed") == "true"
    assert invocations == 1


async def test_streaming_app_raises_mid_stream_releases_slot() -> None:
    """If the app crashes after emitting start + first chunk, the slot must
    be released so a retry runs the handler again. The client sees a
    truncated stream, but ``Idempotency-Key`` semantics survive."""
    invocations = 0

    async def flaky_streaming_app(_scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal invocations
        await _drive_request_lifecycle(receive)
        invocations += 1
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            },
        )
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        if invocations == 1:
            msg = "boom"
            raise RuntimeError(msg)
        await send({"type": "http.response.body", "body": b"b", "more_body": False})

    store = InMemoryStore()
    middleware = IdempotencyMiddleware(flaky_streaming_app, store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        # httpx surfaces the mid-stream raise as a protocol error or as
        # the underlying RuntimeError depending on transport buffering.
        # Partial-body wire delivery is pinned at the unit level.
        with pytest.raises((httpx.RemoteProtocolError, RuntimeError)):
            await client.post(
                "/",
                headers={"Idempotency-Key": "k-flaky"},
                content=b"x",
            )

        assert await store.get(IdempotencyKey("k-flaky")) is None

        second = await client.post(
            "/",
            headers={"Idempotency-Key": "k-flaky"},
            content=b"x",
        )

    assert second.status_code == 200
    assert second.headers.get("idempotency-stored") == "false"  # still a stream
    assert invocations == 2


async def test_handler_returns_without_emitting_start_yields_500() -> None:
    """Handler never sends ``http.response.start`` — middleware synthesizes
    a 500 with ``Idempotency-Stored: false`` and releases the slot."""

    async def silent_app(_scope: Scope, receive: Receive, _send: Send) -> None:
        await _drive_request_lifecycle(receive)

    store = InMemoryStore()
    middleware = IdempotencyMiddleware(silent_app, store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "k-silent"},
            content=b"x",
        )

    assert response.status_code == 500
    assert response.headers.get("idempotency-stored") == "false"
    assert await store.get(IdempotencyKey("k-silent")) is None


async def test_streaming_with_empty_body_first_chunk_still_detected() -> None:
    """``more_body=True`` on an empty first chunk still triggers forwarding mode.

    Defensive: an app that opens the stream eagerly (e.g., for back-pressure
    headers) before producing data should still pass through.
    """
    middleware = IdempotencyMiddleware(
        _make_streaming_app([b"", b"data"]),
        InMemoryStore(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/",
            headers={"Idempotency-Key": "k3"},
            content=b"x",
        )

    assert response.status_code == 200
    assert response.content == b"data"
    assert response.headers.get("idempotency-stored") == "false"


# Framework smoke: Starlette's ``StreamingResponse``  -----------------------
#
# Starlette is a hard runtime dep, so we don't pay a new test
# dependency to verify the canonical streaming primitive on the wire.
# FastAPI's ``StreamingResponse`` re-exports Starlette's, so this also
# covers the FastAPI path implicitly.


async def test_starlette_streaming_response_passes_through() -> None:
    async def chunk_iter() -> Any:
        yield b"a"
        yield b"b"
        yield b"c"

    async def stream_endpoint(_request: Any) -> StreamingResponse:
        return StreamingResponse(chunk_iter(), media_type="text/event-stream")

    app = Starlette(
        routes=[Route("/stream", stream_endpoint, methods=["POST"])],
    )
    middleware = IdempotencyMiddleware(app, InMemoryStore())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/stream",
            headers={"Idempotency-Key": "starlette-stream"},
            content=b"req",
        )

    assert response.status_code == 200
    assert response.content == b"abc"
    assert response.headers.get("idempotency-stored") == "false"
