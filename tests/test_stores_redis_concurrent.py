"""Cross-process concurrent acquire — the distributed-locking proof.

The single-process concurrent test in ``test_stores_conformance.py``
covers ``asyncio.Lock`` (for InMemory) and Lua-atomicity + redis-py
connection-pool behavior (for Redis, but all coros share one client).
This file adds the test that actually justifies the Redis dependency:
N independent OS processes, each with its own client and connection
pool, racing on the same key. Exactly one wins CREATED — the rest see
IN_FLIGHT. That property is what RedisStore exists to provide.

``spawn`` start method (not ``fork``): workers begin with no shared
state from the parent — clean event loop, fresh redis-py pool. Slower
on Linux (~500 ms vs ~50 ms for fork), but sidesteps the fork-of-
asyncio-loop hazards (lingering selectors, half-initialized pools)
that fork inherits.

A ``Manager().Barrier(N)`` synchronizes all N workers right before
``acquire``. Without it the test could pass even when workers run
strictly serially (one finishes before the next imports redis-py) —
the assertions would still hold, but no actual race would have
happened. The barrier converts the test from "probabilistically
covers concurrent claim" to "deterministically proves it".
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import pytest

from _redis_helpers import make_async_client
from fastapi_idempotency import AcquireOutcome, Fingerprint, IdempotencyKey
from fastapi_idempotency.stores.redis import RedisStore

_KEY = "cross-process-acquire"
_FP = "fp-cross"
_TTL = 30.0
_N = 10
_BARRIER_TIMEOUT_S = 10.0


def _acquire_in_subprocess(redis_url: str, barrier: Any) -> str:
    """Worker entry: open own client, sync on barrier, acquire, return outcome.

    Module-level so spawn can pickle it by qualified name. Returns
    ``outcome.value`` (a string) rather than the enum member because
    pickling enums across the process boundary works but is fragile to
    re-imports — strings cross unambiguously.
    """

    async def _run() -> str:
        client = make_async_client(redis_url)
        try:
            store = RedisStore(client)
            # All N workers wait here until everyone has reached this
            # point, then release simultaneously. Without the barrier
            # the test could pass trivially with strictly serial
            # execution; with it, the race is guaranteed.
            barrier.wait(timeout=_BARRIER_TIMEOUT_S)
            result = await store.acquire(
                IdempotencyKey(_KEY),
                Fingerprint(_FP),
                ttl=_TTL,
            )
            return result.outcome.value
        finally:
            await client.aclose()

    return asyncio.run(_run())


@pytest.mark.redis
@pytest.mark.usefixtures("redis_client")
async def test_acquire_serializes_across_processes(redis_url: str) -> None:
    """N OS processes race for one key — exactly one wins CREATED.

    ``redis_client`` is requested via ``usefixtures`` purely for its
    pre-test FLUSHDB on DB 15; the subprocess workers connect by URL
    and don't share the parent's client. The fixture's aclose runs
    after this test independently.
    """
    spawn_ctx = mp.get_context("spawn")
    loop = asyncio.get_running_loop()
    with spawn_ctx.Manager() as manager:
        barrier = manager.Barrier(_N)
        with ProcessPoolExecutor(max_workers=_N, mp_context=spawn_ctx) as pool:
            outcomes = await asyncio.gather(
                *(
                    loop.run_in_executor(pool, _acquire_in_subprocess, redis_url, barrier)
                    for _ in range(_N)
                ),
            )

    counts = Counter(outcomes)
    assert counts[AcquireOutcome.CREATED.value] == 1
    assert counts[AcquireOutcome.IN_FLIGHT.value] == _N - 1
