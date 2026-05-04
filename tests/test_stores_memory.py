"""InMemoryStore-specific tests.

Behavioral conformance against the :class:`Store` protocol lives in
``tests/test_stores_conformance.py`` (parametrized over both backends).
This file holds tests that exercise InMemory-only mechanics:

- Negative-``ttl`` shortcut for expired records — RedisStore's Lua
  ``acquire`` rejects ``ttl_ms <= 0``, so this trick is intentionally
  not portable. Slice 7 (issue #17) will add real-time TTL tests
  covering both backends.
- ``asyncio.Lock``-based concurrency — slice 6 (issue #17) adds the
  same concurrency assertion against ``RedisStore``'s Lua ``EVAL``.
"""

from __future__ import annotations

import asyncio
from collections import Counter

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


async def test_concurrent_acquire_yields_exactly_one_created() -> None:
    store = InMemoryStore()

    results = await asyncio.gather(
        *(store.acquire(KEY, FP, ttl=30.0) for _ in range(10)),
    )

    outcomes = Counter(r.outcome for r in results)
    assert outcomes[AcquireOutcome.CREATED] == 1
    assert outcomes[AcquireOutcome.IN_FLIGHT] == 9
