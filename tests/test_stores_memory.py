"""InMemoryStore-specific tests.

Behavioral conformance against the :class:`Store` protocol lives in
``tests/test_stores_conformance.py`` (parametrized over both backends).
This file holds tests that exercise InMemory-only mechanics —
specifically the negative-``ttl`` shortcut for expired records, which
RedisStore's Lua ``acquire`` rejects (``ttl_ms <= 0``) by design.
"""

from __future__ import annotations

from fastapi_idempotency import (
    AcquireOutcome,
    Fingerprint,
    IdempotencyKey,
    InMemoryStore,
)

KEY = IdempotencyKey("order-memory")
FP = Fingerprint("fp-memory")


async def test_acquire_on_an_expired_record_creates_a_fresh_slot() -> None:
    store = InMemoryStore()
    await store.acquire(KEY, FP, ttl=-1.0)  # already expired

    result = await store.acquire(KEY, FP, ttl=30.0)

    assert result.outcome is AcquireOutcome.CREATED


async def test_get_returns_none_for_an_expired_record() -> None:
    store = InMemoryStore()
    await store.acquire(KEY, FP, ttl=-1.0)

    assert await store.get(KEY) is None
