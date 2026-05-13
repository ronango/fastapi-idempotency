"""Logging-side guarantees: raw idempotency keys must never leak."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx
import pytest

from fastapi_idempotency import (
    Fingerprint,
    IdempotencyKey,
    IdempotencyMiddleware,
    InMemoryStore,
    StoreError,
)
from fastapi_idempotency.middleware import (
    _LOG_HASH_HEX_LEN,
    _hash_for_log,
    _log_context,
)
from fastapi_idempotency.types import AcquireOutcome

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send


# Unit: ``_hash_for_log`` and ``_log_context``  -----------------------------


def test_hash_for_log_returns_12_hex_chars() -> None:
    digest = _hash_for_log(b"some-key", secret=None)

    assert len(digest) == _LOG_HASH_HEX_LEN == 12
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_for_log_known_answer_sha256() -> None:
    """Pin to a precomputed SHA-256 prefix so a future swap to a
    different hash function (e.g., MD5) fails loudly even if length
    and determinism survive."""
    digest = _hash_for_log(b"known-input", secret=None)

    assert digest == "27ae49c070b1"


def test_hash_for_log_is_deterministic() -> None:
    """Log correlation depends on same input producing same hash."""
    a = _hash_for_log(b"order-42", secret=None)
    b = _hash_for_log(b"order-42", secret=None)

    assert a == b


def test_hash_for_log_changes_with_input() -> None:
    a = _hash_for_log(b"order-42", secret=None)
    b = _hash_for_log(b"order-43", secret=None)

    assert a != b


def test_hash_for_log_with_secret_differs_from_plain() -> None:
    """HMAC mode prevents a log-reader from recomputing the hash of a
    guessed key without the secret."""
    plain = _hash_for_log(b"order-42", secret=None)
    hmac_h = _hash_for_log(b"order-42", secret=b"server-secret")

    assert plain != hmac_h


def test_hash_for_log_different_secrets_differ() -> None:
    a = _hash_for_log(b"order-42", secret=b"secret-A")
    b = _hash_for_log(b"order-42", secret=b"secret-B")

    assert a != b


def test_log_context_minimal_has_only_key_hash() -> None:
    ctx = _log_context(IdempotencyKey("k"), secret=None)

    assert set(ctx) == {"key_hash"}


def test_log_context_with_fingerprint_and_outcome() -> None:
    ctx = _log_context(
        IdempotencyKey("k"),
        secret=None,
        fingerprint=Fingerprint("0" * 64),
        outcome=AcquireOutcome.MISMATCH,
    )

    assert ctx["key_hash"] != ""
    assert ctx["fingerprint"] == "0" * _LOG_HASH_HEX_LEN
    assert ctx["outcome"] == "mismatch"


def test_log_context_never_includes_raw_key() -> None:
    """Whatever the future shape of the context dict, the raw key bytes
    must never appear in any value."""
    key = IdempotencyKey("super-secret-order-id-12345")
    ctx = _log_context(
        key,
        secret=None,
        fingerprint=Fingerprint("a" * 64),
        outcome=AcquireOutcome.CREATED,
    )

    for value in ctx.values():
        assert "super-secret" not in value


# Integration: middleware log paths  ----------------------------------------


async def _drive_request(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        if not message.get("more_body", False):
            break


async def echo_200_app(_scope: Scope, receive: Receive, send: Send) -> None:
    await _drive_request(receive)
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        },
    )
    await send({"type": "http.response.body", "body": b"ok"})


_SENSITIVE_KEY = "order-user-pii-9999"


def _key_appears_in_records(
    caplog_records: list[logging.LogRecord],
    raw_key: str,
) -> bool:
    """A leak check: raw key bytes must not appear in any log message,
    args, exc_info, or extra-derived fields."""
    for record in caplog_records:
        if raw_key in record.getMessage():
            return True
        if raw_key in str(record.__dict__):
            return True
    return False


async def test_mismatch_logs_warning_with_hashed_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = IdempotencyMiddleware(echo_200_app, InMemoryStore(), secret=None)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        with caplog.at_level(logging.WARNING, logger="fastapi_idempotency.middleware"):
            await client.post(
                "/",
                headers={"Idempotency-Key": _SENSITIVE_KEY},
                content=b"a",
            )
            await client.post(
                "/",
                headers={"Idempotency-Key": _SENSITIVE_KEY},
                content=b"b",
            )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("different body" in r.getMessage() for r in warnings)
    assert not _key_appears_in_records(caplog.records, _SENSITIVE_KEY)
    # Structured extra fields present on the MISMATCH log.
    mismatch_record = next(r for r in warnings if "different body" in r.getMessage())
    assert mismatch_record.key_hash  # type: ignore[attr-defined]
    assert mismatch_record.outcome == "mismatch"  # type: ignore[attr-defined]
    assert mismatch_record.status == "422"  # type: ignore[attr-defined]


async def test_in_flight_logs_info_with_hashed_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Concurrent 409 path logs at INFO with structured context.

    Two requests race: the first holds the slot via an ``asyncio.Event``
    until the second has had its acquire return IN_FLIGHT.
    """
    proceed = asyncio.Event()

    async def slow_app(_scope: Scope, receive: Receive, send: Send) -> None:
        await _drive_request(receive)
        await proceed.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            },
        )
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = IdempotencyMiddleware(slow_app, InMemoryStore(), secret=None)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        with caplog.at_level(logging.INFO, logger="fastapi_idempotency.middleware"):
            first = asyncio.create_task(
                client.post(
                    "/",
                    headers={"Idempotency-Key": _SENSITIVE_KEY},
                    content=b"x",
                ),
            )
            # Let the first request acquire the slot.
            await asyncio.sleep(0.05)
            second = await client.post(
                "/",
                headers={"Idempotency-Key": _SENSITIVE_KEY},
                content=b"x",
            )
            proceed.set()
            await first

    assert second.status_code == 409
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    inflight_record = next(r for r in info_records if "concurrent request" in r.getMessage())
    assert inflight_record.key_hash  # type: ignore[attr-defined]
    assert inflight_record.outcome == "in_flight"  # type: ignore[attr-defined]
    assert inflight_record.status == "409"  # type: ignore[attr-defined]
    assert not _key_appears_in_records(caplog.records, _SENSITIVE_KEY)


async def test_store_error_logs_warning_with_hashed_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingCompleteStore:
        def __init__(self) -> None:
            self._inner = InMemoryStore()

        async def acquire(self, key, fingerprint, ttl):  # type: ignore[no-untyped-def]
            return await self._inner.acquire(key, fingerprint, ttl)

        async def get(self, key):  # type: ignore[no-untyped-def]
            return await self._inner.get(key)

        async def complete(self, key, response, ttl):  # type: ignore[no-untyped-def]
            msg = "store down"
            raise StoreError(msg)

        async def release(self, key):  # type: ignore[no-untyped-def]
            return await self._inner.release(key)

    middleware = IdempotencyMiddleware(
        echo_200_app,
        FailingCompleteStore(),
        secret=None,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        with caplog.at_level(logging.WARNING, logger="fastapi_idempotency.middleware"):
            response = await client.post(
                "/",
                headers={"Idempotency-Key": _SENSITIVE_KEY},
                content=b"a",
            )

    assert response.status_code == 200
    store_record = next(r for r in caplog.records if "StoreError" in r.getMessage())
    assert store_record.key_hash  # type: ignore[attr-defined]
    assert not _key_appears_in_records(caplog.records, _SENSITIVE_KEY)


async def test_no_response_handler_logs_warning_with_hashed_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The synthesized-500 path (handler returned without sending start)
    must also hash the key — parallel to the handler-exception path."""

    async def silent_app(_scope: Scope, receive: Receive, _send: Send) -> None:
        await _drive_request(receive)
        # Returns without calling send(...).

    middleware = IdempotencyMiddleware(silent_app, InMemoryStore(), secret=None)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        with caplog.at_level(logging.WARNING, logger="fastapi_idempotency.middleware"):
            response = await client.post(
                "/",
                headers={"Idempotency-Key": _SENSITIVE_KEY},
                content=b"x",
            )

    assert response.status_code == 500
    rec = next(r for r in caplog.records if "without sending http.response.start" in r.getMessage())
    assert rec.key_hash  # type: ignore[attr-defined]
    assert rec.status == "500"  # type: ignore[attr-defined]
    assert not _key_appears_in_records(caplog.records, _SENSITIVE_KEY)


async def test_created_and_replay_do_not_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hot path (CREATED + REPLAY) emits no logs — high-volume noise control."""
    middleware = IdempotencyMiddleware(echo_200_app, InMemoryStore(), secret=None)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        with caplog.at_level(logging.DEBUG, logger="fastapi_idempotency.middleware"):
            await client.post(  # CREATED
                "/",
                headers={"Idempotency-Key": "kk"},
                content=b"a",
            )
            await client.post(  # REPLAY
                "/",
                headers={"Idempotency-Key": "kk"},
                content=b"a",
            )

    middleware_records = [r for r in caplog.records if r.name == "fastapi_idempotency.middleware"]
    assert middleware_records == []


async def test_handler_exception_logs_with_hashed_key_no_raw_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom_app(_scope: Scope, receive: Receive, _send: Send) -> None:
        await _drive_request(receive)
        msg = "handler boom"
        raise RuntimeError(msg)

    middleware = IdempotencyMiddleware(boom_app, InMemoryStore(), secret=None)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    ) as client:
        with (
            caplog.at_level(
                logging.WARNING,
                logger="fastapi_idempotency.middleware",
            ),
            pytest.raises((httpx.RemoteProtocolError, RuntimeError)),
        ):
            await client.post(
                "/",
                headers={"Idempotency-Key": _SENSITIVE_KEY},
                content=b"x",
            )

    exc_record = next(
        r for r in caplog.records if "exception inside intercepted handler" in r.getMessage()
    )
    assert exc_record.key_hash  # type: ignore[attr-defined]
    assert exc_record.exc_type == "RuntimeError"  # type: ignore[attr-defined]
    assert not _key_appears_in_records(caplog.records, _SENSITIVE_KEY)


async def test_hmac_secret_changes_logged_hash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify the security property: same key under different secrets
    produces different log hashes — log readers can't recompute hashes
    without the secret."""
    captures: dict[bytes | None, str] = {}
    for secret in (None, b"secret-A", b"secret-B"):
        store = InMemoryStore()
        middleware = IdempotencyMiddleware(echo_200_app, store, secret=secret)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=middleware),
            base_url="http://testserver",
        ) as client:
            caplog.clear()
            with caplog.at_level(
                logging.WARNING,
                logger="fastapi_idempotency.middleware",
            ):
                # Trigger MISMATCH to force a log emission.
                await client.post(
                    "/",
                    headers={"Idempotency-Key": _SENSITIVE_KEY},
                    content=b"a",
                )
                await client.post(
                    "/",
                    headers={"Idempotency-Key": _SENSITIVE_KEY},
                    content=b"b",
                )

        mismatch = next(r for r in caplog.records if "different body" in r.getMessage())
        captures[secret] = mismatch.key_hash  # type: ignore[attr-defined]

    assert captures[None] != captures[b"secret-A"]
    assert captures[b"secret-A"] != captures[b"secret-B"]
