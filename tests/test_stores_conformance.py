"""Cross-store conformance suite — protocol contract enforced for every backend.

Each test runs once per parametrized backend: ``InMemoryStore`` always,
``RedisStore`` when ``@pytest.mark.redis`` is enabled and Docker is
available (gated by the session-scoped ``redis_url`` fixture in
``conftest.py``). If a backend ever drifts from the :class:`Store`
protocol, the entire suite catches it without test duplication.

Slice 6 (concurrency: 10 coros → exactly one CREATED) and slice 7
(TTL expiry under real time) build on this fixture. This slice covers
acquire/get/complete/release classification, identity preservation,
and state-machine edges (complete-after-release, acquire-after-release,
double-release). Expired-record paths defer to slice 7 — InMemory's
negative-TTL trick is rejected by ``RedisStore`` (the Lua acquire script
rejects ``ttl_ms <= 0`` by design), so a real wait under a short positive
TTL is the only portable approach.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from fastapi_idempotency import (
    AcquireOutcome,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyState,
    InMemoryStore,
    Store,
    StoreError,
)
from fastapi_idempotency.stores.redis import RedisStore

if TYPE_CHECKING:
    from collections.abc import Iterator


KEY = IdempotencyKey("order-conformance")
FP = Fingerprint("fp-abc")
RESPONSE = CachedResponse(
    status_code=201,
    headers=(
        (b"content-type", b"application/json"),
        (b"x-trace-id", b"abc-123"),
    ),
    body=b'{"id": 42, "status": "ok"}',
    media_type="application/json",
)


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param("redis", id="redis", marks=pytest.mark.redis),
    ],
)
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    """Yield each Store backend in turn for parametrized conformance.

    Sync (not async) by design: pytest-asyncio cannot resolve an async
    fixture (``redis_client``) via ``getfixturevalue`` from inside
    another async fixture (running event loop conflict). A sync
    accessor lets pytest-asyncio resolve ``redis_client`` cleanly, and
    constructing a ``Store`` requires no awaits.

    The redis branch resolves ``redis_client`` lazily via
    ``getfixturevalue`` so ``pytest -m "not redis"`` deselects the redis
    parameter before the container would start — InMemory-only runs
    don't pay the Docker startup cost.
    """
    if request.param == "memory":
        yield InMemoryStore()
        return

    redis_client = request.getfixturevalue("redis_client")
    yield RedisStore(redis_client)


# ---------------------------------------------------------------------------
# acquire — outcome classification
# ---------------------------------------------------------------------------


async def test_acquire_returns_created_on_first_call(store: Store) -> None:
    result = await store.acquire(KEY, FP, ttl=30.0)

    assert result.outcome is AcquireOutcome.CREATED
    assert result.record.key == KEY
    assert result.record.fingerprint == FP
    assert result.record.state is IdempotencyState.IN_FLIGHT
    assert result.record.response is None
    assert result.record.expires_at > time.time()


async def test_acquire_duplicate_same_fingerprint_returns_in_flight(store: Store) -> None:
    await store.acquire(KEY, FP, ttl=30.0)

    result = await store.acquire(KEY, FP, ttl=30.0)

    assert result.outcome is AcquireOutcome.IN_FLIGHT
    assert result.record.state is IdempotencyState.IN_FLIGHT
    assert result.record.fingerprint == FP
    # IN_FLIGHT is pre-complete: a backend that returned a stale completed
    # record's response here would leak it to a 409-responder.
    assert result.record.response is None


async def test_acquire_different_fingerprint_returns_mismatch(store: Store) -> None:
    await store.acquire(KEY, FP, ttl=30.0)

    result = await store.acquire(KEY, Fingerprint("fp-xyz"), ttl=30.0)

    assert result.outcome is AcquireOutcome.MISMATCH
    assert result.record.fingerprint == FP
    # MISMATCH must not overwrite the holder; the original slot stays put.
    fetched = await store.get(KEY)
    assert fetched is not None
    assert fetched.fingerprint == FP


async def test_acquire_mismatch_after_complete(store: Store) -> None:
    """MISMATCH must supersede REPLAY — privacy boundary across fingerprints.

    Protocol contract (store.py): fingerprint comparison happens inside
    the atomic section. A backend that ordered ``state`` before ``fp``
    would replay a cached response across users with different
    fingerprints — a real cross-tenant leak. Cheap to test, important.
    """
    await store.acquire(KEY, FP, ttl=30.0)
    await store.complete(KEY, RESPONSE, ttl=3600.0)

    result = await store.acquire(KEY, Fingerprint("fp-xyz"), ttl=30.0)

    assert result.outcome is AcquireOutcome.MISMATCH


async def test_acquire_after_complete_returns_replay(store: Store) -> None:
    await store.acquire(KEY, FP, ttl=30.0)
    await store.complete(KEY, RESPONSE, ttl=3600.0)

    result = await store.acquire(KEY, FP, ttl=30.0)

    assert result.outcome is AcquireOutcome.REPLAY
    assert result.record.state is IdempotencyState.COMPLETED
    assert result.record.response == RESPONSE


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_returns_none_for_unknown_key(store: Store) -> None:
    assert await store.get(KEY) is None


async def test_get_returns_record_for_known_key(store: Store) -> None:
    acquired = await store.acquire(KEY, FP, ttl=30.0)

    fetched = await store.get(KEY)

    assert fetched == acquired.record


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


async def test_complete_transitions_state_with_response(store: Store) -> None:
    await store.acquire(KEY, FP, ttl=30.0)

    await store.complete(KEY, RESPONSE, ttl=3600.0)

    record = await store.get(KEY)
    assert record is not None
    assert record.state is IdempotencyState.COMPLETED
    assert record.response == RESPONSE


async def test_complete_preserves_identity(store: Store) -> None:
    """``key``, ``fingerprint``, and ``created_at`` survive the COMPLETED transition."""
    acquired = await store.acquire(KEY, FP, ttl=30.0)
    original = acquired.record

    await store.complete(KEY, RESPONSE, ttl=3600.0)

    record = await store.get(KEY)
    assert record is not None
    assert record.key == original.key
    assert record.fingerprint == original.fingerprint
    assert record.created_at == original.created_at


async def test_complete_raises_on_unknown_key(store: Store) -> None:
    with pytest.raises(StoreError):
        await store.complete(KEY, RESPONSE, ttl=3600.0)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


async def test_release_removes_the_slot(store: Store) -> None:
    await store.acquire(KEY, FP, ttl=30.0)

    await store.release(KEY)

    assert await store.get(KEY) is None


async def test_release_on_missing_key_is_noop(store: Store) -> None:
    await store.release(KEY)  # must not raise


async def test_double_release_is_noop(store: Store) -> None:
    """release is idempotent — middleware error path may double-release."""
    await store.acquire(KEY, FP, ttl=30.0)

    await store.release(KEY)
    await store.release(KEY)  # must not raise


# ---------------------------------------------------------------------------
# state-machine edges — release/complete/acquire interactions
# ---------------------------------------------------------------------------


async def test_complete_after_release_raises(store: Store) -> None:
    """release wipes the slot; subsequent complete has no record to update."""
    await store.acquire(KEY, FP, ttl=30.0)
    await store.release(KEY)

    with pytest.raises(StoreError):
        await store.complete(KEY, RESPONSE, ttl=3600.0)


async def test_acquire_after_release_returns_created(store: Store) -> None:
    """release frees the slot for a fresh acquire (different fingerprint OK)."""
    await store.acquire(KEY, FP, ttl=30.0)
    await store.release(KEY)

    result = await store.acquire(KEY, Fingerprint("fp-different"), ttl=30.0)

    assert result.outcome is AcquireOutcome.CREATED


# ---------------------------------------------------------------------------
# CachedResponse roundtrip — defends the encode/decode contract end-to-end
# ---------------------------------------------------------------------------


async def test_response_roundtrip_preserves_all_fields(store: Store) -> None:
    """Every CachedResponse field survives the codec roundtrip via the store.

    Explicit per-field checks defend against a surprise ``__eq__``
    override on a future CachedResponse subclass — equality could pass
    while a field silently regresses.
    """
    await store.acquire(KEY, FP, ttl=30.0)
    await store.complete(KEY, RESPONSE, ttl=3600.0)

    result = await store.acquire(KEY, FP, ttl=30.0)

    assert result.outcome is AcquireOutcome.REPLAY
    assert result.record.response is not None
    assert result.record.response.status_code == 201
    assert result.record.response.headers == RESPONSE.headers
    assert result.record.response.body == RESPONSE.body
    assert result.record.response.media_type == "application/json"
