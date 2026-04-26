"""Starlette/FastAPI ASGI middleware entrypoint."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeAlias

from starlette.requests import Request

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from .store import Store


ScopeFactory: TypeAlias = Callable[[Request], str | Awaitable[str]]
"""Function that derives a scope string (e.g. tenant, user) from the request.

Called by the middleware before key lookup; the result is prefixed onto
the idempotency key so different scopes don't collide.
"""


class IdempotencyMiddleware:
    """ASGI middleware enforcing ``Idempotency-Key`` semantics.

    Only non-safe methods (POST, PATCH, PUT, DELETE) are intercepted; safe
    requests pass through untouched. Implemented as a raw ASGI class rather
    than :class:`starlette.middleware.base.BaseHTTPMiddleware` so streaming
    response pass-through (v0.2.0) remains possible.
    """

    def __init__(
        self,
        app: ASGIApp,
        store: Store,
        *,
        header_name: str = "Idempotency-Key",
        in_flight_ttl: float = 30.0,
        completed_ttl: float = 86_400.0,
        scope_factory: ScopeFactory | None = None,
    ) -> None:
        self.app = app
        self.store = store
        self.header_name = header_name
        self.in_flight_ttl = in_flight_ttl
        self.completed_ttl = completed_ttl
        self.scope_factory = scope_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError
