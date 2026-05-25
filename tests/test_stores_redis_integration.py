"""Redis-only tests that don't fit the cross-store conformance suite.

Marked ``@pytest.mark.redis``. Two purposes:

- Smoke: a fast-fail connectivity gate before the heavier conformance
  suite spins up.
- Defense-in-depth coverage: backend-specific branches (e.g. the
  Python-side ``is_expired`` filter in ``RedisStore.get``) that
  conformance can't reach because they only fire under sub-millisecond
  races between PEXPIRE and HGET.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
import redis.asyncio
from redis.exceptions import ResponseError

from fastapi_idempotency import (
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
    StoreError,
)
from fastapi_idempotency.stores._serde import encode_record
from fastapi_idempotency.stores.redis import RedisStore


@pytest.mark.redis
async def test_redis_smoke_ping(redis_client: redis.asyncio.Redis) -> None:
    """Smoke: testcontainers Redis is reachable and responsive."""
    assert await redis_client.ping() is True


@pytest.mark.redis
async def test_get_filters_python_side_expired_record(
    redis_client: redis.asyncio.Redis,
) -> None:
    """``RedisStore.get`` returns None for records whose ``expires_at``
    has elapsed by the Python clock, even when PEXPIRE hasn't yet fired
    on the Redis side.

    Synthetic test: writes the hash directly via HSET with a stale
    ``expires_at`` and a long PEXPIRE TTL. PEXPIRE alone would keep
    the key around; only the Python-side ``is_expired`` filter in
    ``get`` (defense-in-depth against the sub-millisecond race
    between PEXPIRE and HGET) returns None.
    """
    store = RedisStore(redis_client)
    key = IdempotencyKey("python-side-expired")
    full_key = store._key(key)

    expired_record = IdempotencyRecord(
        key=key,
        fingerprint=Fingerprint("fp"),
        state=IdempotencyState.IN_FLIGHT,
        created_at=time.time() - 100,
        expires_at=time.time() - 1.0,  # already elapsed by Python clock
        response=None,
    )
    await redis_client.hset(
        full_key,
        mapping={
            b"fp": b"fp",
            b"state": b"in_flight",
            b"data": encode_record(expired_record),
        },
    )
    # Server-side TTL high so PEXPIRE doesn't evict the key — only the
    # Python-clock check inside ``get`` should turn it into None.
    await redis_client.pexpire(full_key, 60_000)

    assert await store.get(key) is None


@pytest.mark.redis
async def test_complete_lua_rejects_fp_mismatch(
    redis_client: redis.asyncio.Redis,
) -> None:
    """The ``_COMPLETE_LUA`` script must refuse to overwrite a slot whose
    stored fp differs from the expected fp the caller passed in.

    Drives the Lua directly with a stale ``expected_fp`` to simulate
    worker A's view of the slot before worker B's re-acquire swapped it.
    """
    store = RedisStore(redis_client)
    key = IdempotencyKey("fp-mismatch-reject")
    full_key = store._key(key)

    fresh = IdempotencyRecord(
        key=key,
        fingerprint=Fingerprint("fp_b"),
        state=IdempotencyState.IN_FLIGHT,
        created_at=time.time(),
        expires_at=time.time() + 30.0,
        response=None,
    )
    await redis_client.hset(
        full_key,
        mapping={
            b"fp": b"fp_b",
            b"state": b"in_flight",
            b"data": encode_record(fresh),
        },
    )
    await redis_client.pexpire(full_key, 60_000)

    stale_data = encode_record(replace(fresh, fingerprint=Fingerprint("fp_a")))

    with pytest.raises(ResponseError, match="reclaimed"):
        await store._complete_script(
            keys=[full_key],
            args=["fp_a", 3600 * 1000, stale_data],
        )

    # Sanity: the fresh slot was not overwritten.
    assert await redis_client.hget(full_key, "fp") == b"fp_b"
    assert await redis_client.hget(full_key, "state") == b"in_flight"


@pytest.mark.redis
async def test_complete_full_path_rejects_eviction_and_reacquire(
    redis_client: redis.asyncio.Redis,
) -> None:
    """End-to-end through ``RedisStore.complete``: an eviction + re-acquire
    that swaps the slot's fp before complete fires raises ``StoreError``
    instead of silently overwriting the fresh slot.

    Guards the Python glue (correct fp passed as ``expected_fp``) on top of
    the Lua-only ``test_complete_lua_rejects_fp_mismatch``. A regression
    that swaps arg order or passes the wrong fingerprint at the call site
    would still pass the Lua test, but must fail this one.
    """
    store = RedisStore(redis_client)
    key = IdempotencyKey("eviction-reacquire-e2e")
    full_key = store._key(key)

    original = IdempotencyRecord(
        key=key,
        fingerprint=Fingerprint("fp_a"),
        state=IdempotencyState.IN_FLIGHT,
        created_at=time.time(),
        expires_at=time.time() + 30.0,
        response=None,
    )

    # Simulate the race: slot was acquired by request A (fp_a), then
    # evicted and re-acquired by request B (fp_b) before A's complete fires.
    reacquired = replace(original, fingerprint=Fingerprint("fp_b"))
    await redis_client.hset(
        full_key,
        mapping={
            b"fp": b"fp_b",
            b"state": b"in_flight",
            b"data": encode_record(reacquired),
        },
    )
    await redis_client.pexpire(full_key, 60_000)

    response = CachedResponse(status_code=200, headers=(), body=b"")
    with pytest.raises(StoreError):
        await store.complete(original, response, ttl=3600.0)

    # fp_b's IN_FLIGHT slot survived intact — no silent overwrite.
    assert await redis_client.hget(full_key, "fp") == b"fp_b"
    assert await redis_client.hget(full_key, "state") == b"in_flight"
