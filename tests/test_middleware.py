"""Acceptance tests for IdempotencyMiddleware.

Tests use httpx.ASGITransport for HTTP cases and direct ASGI calls for
edge cases like non-HTTP scope types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from fastapi_idempotency import IdempotencyMiddleware, InMemoryStore

if TYPE_CHECKING:
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
            "/", headers={"Idempotency-Key": long_key}, content=b"x",
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
            "/", headers={"Idempotency-Key": ""}, content=b"x",
        )

    assert response.status_code == 400
