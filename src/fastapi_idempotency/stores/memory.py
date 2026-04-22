"""In-memory :class:`Store` implementation (single process)."""

from __future__ import annotations

import asyncio
import time

from fastapi_idempotency.types import (
    AcquireOutcome,
    AcquireResult,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
)


class InMemoryStore:
    """Single-process store backed by a dict plus an ``asyncio.Lock``.

    Suitable for tests and single-worker deployments. Not safe across
    worker processes — use :class:`fastapi_idempotency.stores.redis.RedisStore`
    for anything multi-worker.

    Structurally conforms to :class:`fastapi_idempotency.store.Store`.
    """

    def __init__(self) -> None:
        self._records: dict[IdempotencyKey, IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        key: IdempotencyKey,
        fingerprint: Fingerprint,
        ttl: float,
    ) -> AcquireResult:
        async with self._lock:
            now = time.time()
            record = IdempotencyRecord(
                key=key,
                fingerprint=fingerprint,
                state=IdempotencyState.IN_FLIGHT,
                created_at=now,
                expires_at=now + ttl,
                response=None,
            )
            self._records[key] = record
            return AcquireResult(outcome=AcquireOutcome.CREATED, record=record)

    async def get(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        async with self._lock:
            return self._records.get(key)

    async def complete(
        self,
        key: IdempotencyKey,
        response: CachedResponse,
        ttl: float,
    ) -> None:
        raise NotImplementedError

    async def release(self, key: IdempotencyKey) -> None:
        async with self._lock:
            self._records.pop(key, None)
