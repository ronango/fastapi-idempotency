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

from fastapi_idempotency import AcquireOutcome
from fastapi_idempotency.stores import redis as redis_module
from fastapi_idempotency.stores.redis import _ACQUIRE_LUA, RedisStore


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
