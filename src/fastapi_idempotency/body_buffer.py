"""Buffering helpers for ASGI request bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi_idempotency.errors import RequestTooLargeError

if TYPE_CHECKING:
    from starlette.types import Message, Receive


async def buffer_request_body(
    receive: Receive,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes, Receive]:
    """Drain ASGI ``receive()`` and return ``(body, replay_receive)``.

    Middleware must fingerprint the body before the handler consumes it;
    ``receive()`` is one-shot, so we drain and hand the handler a replay
    that yields the same body once.

    Raises :class:`RequestTooLargeError` as soon as the running total
    exceeds ``max_bytes``; the rest of the stream is left untouched so
    the caller can decide what to do (typically respond 413 without
    reading further).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk: bytes = message.get("body", b"")
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise RequestTooLargeError(
                f"request body exceeds {max_bytes} bytes",
            )
        chunks.append(chunk)
        if not message.get("more_body", False):
            break

    body = b"".join(chunks)
    return body, _Replay(body)


class _Replay:
    """One-shot ASGI ``receive`` yielding a buffered body once."""

    def __init__(self, body: bytes) -> None:
        self._message: Message = {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }
        self._consumed = False

    async def __call__(self) -> Message:
        if self._consumed:
            raise RuntimeError("replay receive() called twice")
        self._consumed = True
        return self._message
