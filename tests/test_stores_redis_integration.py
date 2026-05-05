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

import pytest
import redis.asyncio

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
async def test_complete_raises_on_python_side_expired_record(
    redis_client: redis.asyncio.Redis,
) -> None:
    """``RedisStore.complete`` raises ``StoreError`` when the existing
    record is expired by Python clock, even if PEXPIRE hasn't fired.

    Symmetric with ``test_get_filters_python_side_expired_record``: the
    in-flight TTL race that masks an expired slot from server-side
    eviction is the exact scenario the defense exists to catch — without
    it, ``complete`` would silently overwrite an "expired but still in
    Redis" slot, exactly the bug fixed in InMemoryStore in slice 7.
    """
    store = RedisStore(redis_client)
    key = IdempotencyKey("python-side-expired-complete")
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
    await redis_client.pexpire(full_key, 60_000)  # PEXPIRE not yet fired

    response = CachedResponse(status_code=200, headers=(), body=b"")
    with pytest.raises(StoreError):
        await store.complete(key, response, ttl=3600.0)
