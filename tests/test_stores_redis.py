"""Tests for the Redis store module that don't require a live Redis.

The cross-store conformance suite (slice 5) covers the actual store
behavior against a testcontainers Redis. The tests here pin module-level
invariants — namespace defaults, the Lua/enum agreement, the
``decode_responses`` guard — so they catch regressions without needing
container infrastructure.
"""

from __future__ import annotations

import re

import pytest
import redis.asyncio

from fastapi_idempotency import AcquireOutcome, IdempotencyKey, StoreError
from fastapi_idempotency.stores import redis as redis_module
from fastapi_idempotency.stores.redis import (
    _ACQUIRE_LUA,
    RedisStore,
    _wrap_redis_error,
)


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
