"""Cross-store conformance suite — protocol contract enforced for every backend.

Each test runs once per parametrized backend: ``InMemoryStore`` always,
``RedisStore`` when ``@pytest.mark.redis`` is enabled and Docker is
available (gated by the session-scoped ``redis_url`` fixture in
``conftest.py``). If a backend ever drifts from the :class:`Store`
protocol, the entire suite catches it without test duplication.

Slice 6 added single-process concurrency. Slice 7 (this) adds TTL
expiry tests under real ``asyncio.sleep`` — InMemory's negative-TTL
trick is rejected by ``RedisStore`` (the Lua acquire script rejects
``ttl_ms <= 0`` by design), so a real wait under a short positive TTL
is the only portable way to exercise expiry across both backends.
TTL tests carry ``@pytest.mark.slow`` so quick dev runs can deselect
them with ``pytest -m "not slow"``.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import TYPE_CHECKING

import httpx
import pytest

from fastapi_idempotency import (
    AcquireOutcome,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyMiddleware,
    IdempotencyRecord,
    IdempotencyState,
    InMemoryStore,
    Store,
    StoreError,
)
from fastapi_idempotency.stores.redis import RedisStore

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import Receive, Scope, Send


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


def _record(
    *,
    key: IdempotencyKey = KEY,
    fingerprint: Fingerprint = FP,
    ttl: float = 30.0,
) -> IdempotencyRecord:
    """Synthesize an IN_FLIGHT record for error-path tests only.

    The ``Store.complete`` contract says ``record`` is an opaque
    token from ``acquire``. This helper deliberately bypasses that
    contract to exercise error paths where no slot exists; do not
    use it in tests that drive happy-path semantics. Keyword-only
    because ``key`` and ``fingerprint`` are both str-flavoured
    NewTypes — positional order errors don't type-check.
    """
    now = time.time()
    return IdempotencyRecord(
        key=key,
        fingerprint=fingerprint,
        state=IdempotencyState.IN_FLIGHT,
        created_at=now,
        expires_at=now + ttl,
        response=None,
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
    acquired = await store.acquire(KEY, FP, ttl=30.0)
    await store.complete(acquired.record, RESPONSE, ttl=3600.0)

    result = await store.acquire(KEY, Fingerprint("fp-xyz"), ttl=30.0)

    assert result.outcome is AcquireOutcome.MISMATCH


async def test_acquire_after_complete_returns_replay(store: Store) -> None:
    acquired = await store.acquire(KEY, FP, ttl=30.0)
    await store.complete(acquired.record, RESPONSE, ttl=3600.0)

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
    acquired = await store.acquire(KEY, FP, ttl=30.0)

    await store.complete(acquired.record, RESPONSE, ttl=3600.0)

    record = await store.get(KEY)
    assert record is not None
    assert record.state is IdempotencyState.COMPLETED
    assert record.response == RESPONSE


async def test_complete_preserves_identity(store: Store) -> None:
    """``key``, ``fingerprint``, and ``created_at`` survive the COMPLETED transition."""
    acquired = await store.acquire(KEY, FP, ttl=30.0)
    original = acquired.record

    await store.complete(original, RESPONSE, ttl=3600.0)

    record = await store.get(KEY)
    assert record is not None
    assert record.key == original.key
    assert record.fingerprint == original.fingerprint
    assert record.created_at == original.created_at


async def test_complete_raises_on_unknown_key(store: Store) -> None:
    with pytest.raises(StoreError):
        await store.complete(_record(), RESPONSE, ttl=3600.0)


async def test_complete_rejects_fingerprint_mismatch(store: Store) -> None:
    """Both backends must reject when the stored fp differs from the
    caller's record. Without this guard a late ``complete`` would
    overwrite a slot already re-acquired by another request.
    """
    original = (await store.acquire(KEY, FP, ttl=30.0)).record
    await store.release(KEY)
    await store.acquire(KEY, Fingerprint("fp-other"), ttl=30.0)

    with pytest.raises(StoreError):
        await store.complete(original, RESPONSE, ttl=3600.0)

    surviving = await store.get(KEY)
    assert surviving is not None
    assert surviving.fingerprint == Fingerprint("fp-other")
    assert surviving.state is IdempotencyState.IN_FLIGHT


async def test_complete_preserves_callers_created_at_under_same_fp_aba(
    store: Store,
) -> None:
    """Same-fp ABA: both backends persist the *caller's* ``created_at``.

    Pinned cross-backend so the accepted residual stays observable and
    consistent — without this, InMemory and Redis could silently diverge
    on which acquire-time survives.
    """
    original = (await store.acquire(KEY, FP, ttl=30.0)).record
    await store.release(KEY)
    # Force a measurable gap so the second acquire's ``created_at``
    # differs from the first — otherwise sub-microsecond ticks could
    # mask a backend that silently picks the wrong timestamp.
    await asyncio.sleep(0.01)
    reacquired = (await store.acquire(KEY, FP, ttl=30.0)).record
    assert reacquired.created_at > original.created_at

    await store.complete(original, RESPONSE, ttl=3600.0)

    stored = await store.get(KEY)
    assert stored is not None
    assert stored.created_at == original.created_at


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
    acquired = await store.acquire(KEY, FP, ttl=30.0)
    await store.release(KEY)

    with pytest.raises(StoreError):
        await store.complete(acquired.record, RESPONSE, ttl=3600.0)


async def test_acquire_after_release_returns_created(store: Store) -> None:
    """release frees the slot for a fresh acquire (different fingerprint OK)."""
    await store.acquire(KEY, FP, ttl=30.0)
    await store.release(KEY)

    result = await store.acquire(KEY, Fingerprint("fp-different"), ttl=30.0)

    assert result.outcome is AcquireOutcome.CREATED


# ---------------------------------------------------------------------------
# concurrency — single-process race
# ---------------------------------------------------------------------------


async def test_concurrent_acquire_yields_exactly_one_created(store: Store) -> None:
    """Single-process race on one key — exactly one wins CREATED.

    Tests two different mechanisms in one shape:

    - InMemoryStore: ``asyncio.Lock`` serializes critical sections in
      the single event loop.
    - RedisStore: the Lua ``acquire`` script is atomic on the Redis
      server side (Redis is single-threaded for command execution),
      so even N coroutines sharing one connection pool can't double-claim.

    Cross-process atomicity (the property that justifies RedisStore over
    InMemoryStore) is covered by ``test_stores_redis_concurrent.py``.
    """
    n = 10
    results = await asyncio.gather(
        *(store.acquire(KEY, FP, ttl=30.0) for _ in range(n)),
    )

    outcomes = Counter(r.outcome for r in results)
    assert outcomes[AcquireOutcome.CREATED] == 1
    assert outcomes[AcquireOutcome.IN_FLIGHT] == n - 1


# ---------------------------------------------------------------------------
# TTL expiry — real-time wait, only portable approach across backends
# ---------------------------------------------------------------------------

# 0.5s TTL + 0.7s sleep gives 0.2s margin — safe against CI jitter and
# asyncio.sleep granularity. Lower would be faster but flaky.
_TTL_S = 0.5
_TTL_WAIT_S = 0.7


@pytest.mark.slow
async def test_get_returns_none_after_ttl_expires(store: Store) -> None:
    """``get`` filters expired records — InMemory by Python clock,
    Redis by PEXPIRE plus a Python-side ``is_expired`` defense in depth."""
    await store.acquire(KEY, FP, ttl=_TTL_S)
    await asyncio.sleep(_TTL_WAIT_S)

    assert await store.get(KEY) is None


@pytest.mark.slow
async def test_acquire_after_ttl_expires_creates_fresh_slot(store: Store) -> None:
    """Expired slots are reclaimed by the next ``acquire`` (lazy GC)."""
    await store.acquire(KEY, FP, ttl=_TTL_S)
    await asyncio.sleep(_TTL_WAIT_S)

    result = await store.acquire(KEY, FP, ttl=30.0)

    assert result.outcome is AcquireOutcome.CREATED


@pytest.mark.slow
async def test_completed_record_expires_with_completed_ttl(store: Store) -> None:
    """``complete``'s ``ttl`` argument actually drives expiry — separate
    knob from ``acquire``'s in-flight ttl. Operators rely on this for
    tuning replay-window length without touching in-flight semantics."""
    acquired = await store.acquire(KEY, FP, ttl=30.0)
    await store.complete(acquired.record, RESPONSE, ttl=_TTL_S)
    await asyncio.sleep(_TTL_WAIT_S)

    assert await store.get(KEY) is None


@pytest.mark.slow
async def test_complete_after_ttl_expires_raises(store: Store) -> None:
    """Production race: in-flight TTL elapses before the handler finishes.

    Both backends must raise — Redis via the Lua's missing-key check
    after PEXPIRE eviction, InMemory via the ``is_expired`` guard.
    Without this conformance test InMemory used to silently overwrite
    the expired slot with a fresh COMPLETED record, hiding the
    ``in_flight_ttl`` tuning bug operators are supposed to see.
    """
    acquired = await store.acquire(KEY, FP, ttl=_TTL_S)
    await asyncio.sleep(_TTL_WAIT_S)

    with pytest.raises(StoreError):
        await store.complete(acquired.record, RESPONSE, ttl=3600.0)


@pytest.mark.slow
async def test_complete_rejects_fp_mismatch_after_ttl_reacquire(store: Store) -> None:
    """Full long-handler race via real TTL eviction.

    Distinct from ``test_complete_rejects_fingerprint_mismatch`` (which
    uses explicit ``release``) — this one drives the production scenario
    end-to-end: A's slot evicts by TTL, B re-acquires with a different
    fingerprint, A's late ``complete`` must raise rather than overwrite
    B's slot. Catches a backend that handled ``release``-then-reacquire
    correctly but mishandled TTL-eviction-then-reacquire — Redis's
    PEXPIRE eviction is a different path from InMemory's lazy ``is_expired``
    check, and both must converge on the same fp-mismatch verdict.
    """
    acquired = await store.acquire(KEY, FP, ttl=_TTL_S)
    await asyncio.sleep(_TTL_WAIT_S)
    await store.acquire(KEY, Fingerprint("fp-other"), ttl=30.0)

    with pytest.raises(StoreError):
        await store.complete(acquired.record, RESPONSE, ttl=3600.0)

    surviving = await store.get(KEY)
    assert surviving is not None
    assert surviving.fingerprint == Fingerprint("fp-other")
    assert surviving.state is IdempotencyState.IN_FLIGHT


# ---------------------------------------------------------------------------
# CachedResponse roundtrip — defends the encode/decode contract end-to-end
# ---------------------------------------------------------------------------


async def test_response_roundtrip_preserves_all_fields(store: Store) -> None:
    """Every CachedResponse field survives the codec roundtrip via the store.

    Explicit per-field checks defend against a surprise ``__eq__``
    override on a future CachedResponse subclass — equality could pass
    while a field silently regresses.
    """
    acquired = await store.acquire(KEY, FP, ttl=30.0)
    await store.complete(acquired.record, RESPONSE, ttl=3600.0)

    result = await store.acquire(KEY, FP, ttl=30.0)

    assert result.outcome is AcquireOutcome.REPLAY
    assert result.record.response is not None
    assert result.record.response.status_code == 201
    assert result.record.response.headers == RESPONSE.headers
    assert result.record.response.body == RESPONSE.body
    assert result.record.response.media_type == "application/json"


# ---------------------------------------------------------------------------
# scope_factory — scoping is store-agnostic, so it must behave identically
# on every backend (scoped keys round-trip through the real Redis key space)
# ---------------------------------------------------------------------------


async def _echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Minimal ASGI app echoing the request body, or 'ok' if empty."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = b"".join(chunks) or b"ok"
    await send(
        {"type": "http.response.start", "status": 200, "headers": []},
    )
    await send({"type": "http.response.body", "body": body})


def _scoped_client(store: Store) -> httpx.AsyncClient:
    """ASGI client whose middleware scopes the key by ``X-Tenant-Id``."""
    middleware = IdempotencyMiddleware(
        _echo_app,
        store,
        secret=None,
        scope_factory=lambda req: req.headers["x-tenant-id"].encode(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url="http://testserver",
    )


async def test_scope_factory_different_scope_isolates_across_backends(store: Store) -> None:
    """Same key + same body, different scope → two independent CREATEDs.

    Runs against every backend: the scoped keys must not collide once
    they round-trip through the real store key space.
    """
    async with _scoped_client(store) as client:
        first = await client.post(
            "/",
            headers={"Idempotency-Key": "abc", "X-Tenant-Id": "tenant-a"},
            content=b"hi",
        )
        second = await client.post(
            "/",
            headers={"Idempotency-Key": "abc", "X-Tenant-Id": "tenant-b"},
            content=b"hi",
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert "idempotent-replayed" not in second.headers
    # Fresh handler output on the second tenant — not a replayed body.
    assert second.text == "hi"


async def test_scope_factory_same_scope_replays_across_backends(store: Store) -> None:
    """Same key + same body + same scope → CREATED then REPLAY, every backend."""
    async with _scoped_client(store) as client:
        headers = {"Idempotency-Key": "abc", "X-Tenant-Id": "tenant-a"}
        first = await client.post("/", headers=headers, content=b"hi")
        replay = await client.post("/", headers=headers, content=b"hi")

    assert first.status_code == 200
    assert replay.headers.get("idempotent-replayed") == "true"
