"""Tests for buffer_request_body."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from starlette.types import Message

from fastapi_idempotency.body_buffer import buffer_request_body
from fastapi_idempotency.errors import RequestTooLargeError

Receive = Callable[[], Awaitable[Message]]


async def _to_aiter(items: list[Message]) -> AsyncIterator[Message]:
    for item in items:
        yield item


def make_receive(messages: list[Message]) -> Receive:
    """Fake ASGI receive() that yields the given messages in order."""
    iterator = _to_aiter(messages)

    async def receive() -> Message:
        return await anext(iterator)

    return receive


async def test_empty_body() -> None:
    receive = make_receive([{"type": "http.request", "body": b"", "more_body": False}])

    body, replay = await buffer_request_body(receive)

    assert body == b""
    assert await replay() == {"type": "http.request", "body": b"", "more_body": False}


async def test_single_chunk_body() -> None:
    receive = make_receive(
        [{"type": "http.request", "body": b'{"id":1}', "more_body": False}],
    )

    body, replay = await buffer_request_body(receive)

    assert body == b'{"id":1}'
    replayed = await replay()
    assert replayed["body"] == b'{"id":1}'
    assert replayed["more_body"] is False


async def test_multi_chunk_body_concatenates_in_order() -> None:
    receive = make_receive(
        [
            {"type": "http.request", "body": b"hel", "more_body": True},
            {"type": "http.request", "body": b"lo, ", "more_body": True},
            {"type": "http.request", "body": b"world", "more_body": False},
        ],
    )

    body, replay = await buffer_request_body(receive)

    assert body == b"hello, world"
    replayed = await replay()
    assert replayed["body"] == b"hello, world"
    assert replayed["more_body"] is False


async def test_max_bytes_raises_request_too_large() -> None:
    receive = make_receive(
        [{"type": "http.request", "body": b"x" * 200, "more_body": False}],
    )

    with pytest.raises(RequestTooLargeError):
        await buffer_request_body(receive, max_bytes=128)


async def test_max_bytes_stops_reading_at_overrun() -> None:
    """Subsequent chunks must not be consumed once we've raised."""
    consumed: list[bytes] = []

    async def receive() -> Message:
        if not consumed:
            consumed.append(b"first")
            return {"type": "http.request", "body": b"x" * 100, "more_body": True}
        consumed.append(b"second")
        return {"type": "http.request", "body": b"y", "more_body": False}

    with pytest.raises(RequestTooLargeError):
        await buffer_request_body(receive, max_bytes=50)

    assert consumed == [b"first"]


async def test_max_bytes_at_exactly_the_limit_passes() -> None:
    receive = make_receive(
        [{"type": "http.request", "body": b"x" * 100, "more_body": False}],
    )

    body, _ = await buffer_request_body(receive, max_bytes=100)

    assert len(body) == 100


async def test_replay_raises_on_double_consume() -> None:
    receive = make_receive(
        [{"type": "http.request", "body": b"x", "more_body": False}],
    )
    _, replay = await buffer_request_body(receive)

    await replay()
    with pytest.raises(RuntimeError):
        await replay()


async def test_disconnect_mid_stream_returns_partial_body() -> None:
    receive = make_receive(
        [
            {"type": "http.request", "body": b"part", "more_body": True},
            {"type": "http.disconnect"},
        ],
    )

    body, replay = await buffer_request_body(receive)

    assert body == b"part"
    assert (await replay())["body"] == b"part"


async def test_immediate_disconnect_yields_empty_body() -> None:
    receive = make_receive([{"type": "http.disconnect"}])

    body, _ = await buffer_request_body(receive)

    assert body == b""
