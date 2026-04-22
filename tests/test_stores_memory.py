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


async def test_acquire_returns_created_on_first_call() -> None:
    store = InMemoryStore()
    key = IdempotencyKey("order-123")
    fingerprint = Fingerprint("abc")

    result = await store.acquire(key, fingerprint, ttl=30.0)

    assert result.outcome is AcquireOutcome.CREATED
    assert result.record.key == key
    assert result.record.fingerprint == fingerprint
    assert result.record.state is IdempotencyState.IN_FLIGHT
    assert result.record.response is None
    assert result.record.expires_at > time.time()
