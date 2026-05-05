"""Tests for the Redis store module that don't require a live Redis.

The cross-store conformance suite (slice 5) covers the actual store
behavior against a testcontainers Redis. The tests here pin module-level
invariants — namespace defaults, the Lua/enum agreement, the
``decode_responses`` guard, and per-method error-wrapping — so they
catch regressions without needing container infrastructure.
"""

from __future__ import annotations

import re
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio
from redis.exceptions import RedisError

from fastapi_idempotency import (
    AcquireOutcome,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    StoreError,
)
from fastapi_idempotency.stores import redis as redis_module
from fastapi_idempotency.stores._serde import encode_record
from fastapi_idempotency.stores.redis import (
    _ACQUIRE_LUA,
    RedisStore,
    _wrap_redis_error,
)
from fastapi_idempotency.types import IdempotencyRecord, IdempotencyState


def test_redis_store_module_imports() -> None:
    """Module must import cleanly. Sanity that top-level imports work."""
    assert redis_module.RedisStore is not None


def test_namespace_default_is_idem() -> None:
    """Default namespace per docstring is 'idem'."""
    client = redis.asyncio.Redis()  # no connect; constructor only
    store = RedisStore(client)

    assert store.namespace == "idem"


def test_namespace_can_be_overridden() -> None:
    client = redis.asyncio.Redis()

    store = RedisStore(client, namespace="myapp")

    assert store.namespace == "myapp"


def test_constructor_rejects_decode_responses_true() -> None:
    """A client with ``decode_responses=True`` would silently corrupt
    the msgpack codec — fail loud at construction instead."""
    client = redis.asyncio.Redis(decode_responses=True)

    with pytest.raises(ValueError, match="decode_responses=False"):
        RedisStore(client)


def test_lua_outcome_strings_match_enum_values() -> None:
    """The Lua acquire script returns one of four outcome strings.

    These literals MUST match :class:`AcquireOutcome`'s ``.value`` strings
    exactly, otherwise a rename in the enum will silently break the
    acquire-result decode in production.
    """
    found = set(re.findall(r"return \{'([a-z_]+)'", _ACQUIRE_LUA))
    expected = {outcome.value for outcome in AcquireOutcome}

    assert found == expected


def test_namespacing_uses_default_prefix() -> None:
    """Stored Redis keys are prefixed with ``{namespace}:``."""
    client = redis.asyncio.Redis()
    store = RedisStore(client)

    # ``_key`` is private, but the formatting is part of the wire contract
    # operators rely on for Redis introspection (KEYS pattern, SCAN, etc.).
    assert store._key(IdempotencyKey("abc-123")) == "idem:abc-123"


def test_namespacing_uses_custom_prefix() -> None:
    client = redis.asyncio.Redis()
    store = RedisStore(client, namespace="myapp")

    assert store._key(IdempotencyKey("abc-123")) == "myapp:abc-123"


def test_wrap_redis_error_returns_store_error_with_op_in_message() -> None:
    """Each method's except sites translate redis-py exceptions to StoreError
    with the op name in the message — operators get a breadcrumb without
    parsing the traceback."""
    err = _wrap_redis_error("acquire", RuntimeError("connection refused"))

    assert isinstance(err, StoreError)
    assert "Redis acquire failed" in str(err)
    assert "connection refused" in str(err)


# ---------------------------------------------------------------------------
# Fault-injection: per-method error wrapping
#
# Each ``except (RedisError, OSError)`` site in ``RedisStore`` translates
# the underlying exception into a ``StoreError`` carrying its own op name.
# Without these tests the op-name strings (``"acquire"``, ``"get"``,
# ``"complete"``, ``"release"``) are invisible to CI — a typo there only
# surfaces when an operator reads a 5-line stack trace at 03:00. Stub
# clients let us probe the wrapping contract per call site without
# spinning up Redis.
# ---------------------------------------------------------------------------


def _stub_client(
    *,
    register_script_returns: tuple[Any, Any] | None = None,
    hget_returns: Any = None,
    hget_raises: BaseException | None = None,
    delete_raises: BaseException | None = None,
) -> MagicMock:
    """Build a minimal ``redis.asyncio.Redis``-shaped stub.

    ``register_script_returns`` is a 2-tuple corresponding to the two
    ``register_script`` calls in ``RedisStore.__init__`` (acquire then
    complete). ``hget_returns``/``hget_raises`` configure the shared
    ``hget`` mock; ``delete_raises`` configures ``delete``.
    """
    client = MagicMock()
    encoder = MagicMock()
    encoder.decode_responses = False
    client.get_encoder = MagicMock(return_value=encoder)
    if register_script_returns is None:
        client.register_script = MagicMock(side_effect=[AsyncMock(), AsyncMock()])
    else:
        client.register_script = MagicMock(side_effect=list(register_script_returns))
    if hget_raises is not None:
        client.hget = AsyncMock(side_effect=hget_raises)
    else:
        client.hget = AsyncMock(return_value=hget_returns)
    if delete_raises is not None:
        client.delete = AsyncMock(side_effect=delete_raises)
    else:
        client.delete = AsyncMock(return_value=1)
    return client


KEY = IdempotencyKey("k")
FP = Fingerprint("f")
RESPONSE = CachedResponse(status_code=200, headers=(), body=b"")


def _existing_record_bytes() -> bytes:
    """An IN_FLIGHT record with future ``expires_at`` for complete-path setup."""
    return encode_record(
        IdempotencyRecord(
            key=KEY,
            fingerprint=FP,
            state=IdempotencyState.IN_FLIGHT,
            created_at=time.time(),
            expires_at=time.time() + 30.0,
            response=None,
        ),
    )


async def test_acquire_wraps_redis_error_with_op_name() -> None:
    acquire_script = AsyncMock(side_effect=RedisError("boom"))
    client = _stub_client(register_script_returns=(acquire_script, AsyncMock()))
    store = RedisStore(client)

    with pytest.raises(StoreError, match="Redis acquire failed"):
        await store.acquire(KEY, FP, ttl=30.0)


async def test_get_wraps_redis_error_with_op_name() -> None:
    client = _stub_client(hget_raises=RedisError("boom"))
    store = RedisStore(client)

    with pytest.raises(StoreError, match="Redis get failed"):
        await store.get(KEY)


async def test_complete_wraps_hget_redis_error_with_op_name() -> None:
    client = _stub_client(hget_raises=RedisError("boom"))
    store = RedisStore(client)

    with pytest.raises(StoreError, match="Redis complete failed"):
        await store.complete(KEY, RESPONSE, ttl=3600.0)


async def test_complete_wraps_script_redis_error_with_op_name() -> None:
    """The complete path has TWO redis-py call sites (HGET then EVAL).
    Each must wrap independently — covers the second except path."""
    complete_script = AsyncMock(side_effect=RedisError("boom"))
    client = _stub_client(
        register_script_returns=(AsyncMock(), complete_script),
        hget_returns=_existing_record_bytes(),
    )
    store = RedisStore(client)

    with pytest.raises(StoreError, match="Redis complete failed"):
        await store.complete(KEY, RESPONSE, ttl=3600.0)


async def test_release_wraps_redis_error_with_op_name() -> None:
    client = _stub_client(delete_raises=RedisError("boom"))
    store = RedisStore(client)

    with pytest.raises(StoreError, match="Redis release failed"):
        await store.release(KEY)


async def test_acquire_wraps_oserror_with_op_name() -> None:
    """The except clause catches both RedisError and OSError — the second
    half (OSError, e.g. socket-level errors) needs its own probe.
    Picking acquire as the representative; the other 4 use identical
    ``except (RedisError, OSError)`` shape."""
    acquire_script = AsyncMock(side_effect=OSError("connection refused"))
    client = _stub_client(register_script_returns=(acquire_script, AsyncMock()))
    store = RedisStore(client)

    with pytest.raises(StoreError, match="Redis acquire failed"):
        await store.acquire(KEY, FP, ttl=30.0)
