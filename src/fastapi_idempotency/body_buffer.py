"""Buffering helpers for ASGI request bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import Message, Receive


async def buffer_request_body(receive: Receive) -> tuple[bytes, Receive]:
    """Drain ASGI ``receive()`` and return ``(body, replay_receive)``.

    Middleware must fingerprint the body before the handler consumes it;
    ``receive()`` is one-shot, so we drain and hand the handler a replay
    that yields the same body once.
    """
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break

    body = b"".join(chunks)
    return body, _make_replay(body)


def _make_replay(body: bytes) -> Receive:
    async def replay() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    return replay
