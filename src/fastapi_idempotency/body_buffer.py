"""Buffering helpers for ASGI request bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import Receive


async def buffer_request_body(receive: Receive) -> tuple[bytes, Receive]:
    """Drain the ASGI ``receive()`` stream and return ``(body, replay_receive)``.

    The returned ``replay_receive`` yields the buffered body downstream so
    the wrapped handler sees an unchanged request. Buffering is required
    because the middleware must fingerprint the body *before* the handler
    consumes it.
    """
    raise NotImplementedError
