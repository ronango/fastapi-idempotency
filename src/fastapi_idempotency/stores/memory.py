"""In-memory :class:`Store` implementation (single process)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from fastapi_idempotency.errors import StoreError
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
            existing = self._records.get(key)
            if existing is not None and existing.is_expired(now):
                existing = None

            if existing is None:
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

            # Plain ``!=`` is fine: both fingerprints are server-computed
            # over attacker-supplied body, so timing leaks no useful bits.
            if existing.fingerprint != fingerprint:
                return AcquireResult(outcome=AcquireOutcome.MISMATCH, record=existing)

            if existing.state is IdempotencyState.IN_FLIGHT:
                return AcquireResult(outcome=AcquireOutcome.IN_FLIGHT, record=existing)
            return AcquireResult(outcome=AcquireOutcome.REPLAY, record=existing)

    async def get(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        async with self._lock:
            record = self._records.get(key)
            if record is None or record.is_expired(time.time()):
                return None
            return record

    async def complete(
        self,
        key: IdempotencyKey,
        response: CachedResponse,
        ttl: float,
    ) -> None:
        async with self._lock:
            now = time.time()
            existing = self._records.get(key)
            # Expired-but-not-yet-purged records count as gone (matches
            # Redis PEXPIRE eviction). Must run inside ``self._lock`` —
            # outside, a TOCTOU race could silent-overwrite a fresh slot.
            if existing is None or existing.is_expired(now):
                raise StoreError(
                    f"cannot complete unknown idempotency key: {key!r}",
                )
            self._records[key] = replace(
                existing,
                state=IdempotencyState.COMPLETED,
                expires_at=now + ttl,
                response=response,
            )

    async def release(self, key: IdempotencyKey) -> None:
        async with self._lock:
            self._records.pop(key, None)
