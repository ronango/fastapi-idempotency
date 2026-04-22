"""Acceptance tests for InMemoryStore."""

from __future__ import annotations

import time

from fastapi_idempotency import (
    AcquireOutcome,
    Fingerprint,
    IdempotencyKey,
    IdempotencyState,
    InMemoryStore,
)

KEY = IdempotencyKey("order-123")
FP = Fingerprint("abc")


async def test_acquire_returns_created_on_first_call() -> None:
    store = InMemoryStore()

    result = await store.acquire(KEY, FP, ttl=30.0)

    assert result.outcome is AcquireOutcome.CREATED
    assert result.record.key == KEY
    assert result.record.fingerprint == FP
    assert result.record.state is IdempotencyState.IN_FLIGHT
    assert result.record.response is None
    assert result.record.expires_at > time.time()


async def test_get_returns_none_for_unknown_key() -> None:
    store = InMemoryStore()

    assert await store.get(KEY) is None


async def test_get_returns_the_record_for_a_known_key() -> None:
    store = InMemoryStore()
    acquired = await store.acquire(KEY, FP, ttl=30.0)

    fetched = await store.get(KEY)

    assert fetched == acquired.record


async def test_release_removes_an_in_flight_slot() -> None:
    store = InMemoryStore()
    await store.acquire(KEY, FP, ttl=30.0)

    await store.release(KEY)

    assert await store.get(KEY) is None


async def test_release_on_a_missing_key_is_a_noop() -> None:
    store = InMemoryStore()

    await store.release(KEY)  # must not raise
