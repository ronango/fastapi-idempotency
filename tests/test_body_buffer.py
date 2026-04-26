"""Tests for buffer_request_body."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from starlette.types import Message

from fastapi_idempotency.body_buffer import buffer_request_body

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
