"""In-memory :class:`Store` implementation (single process)."""

from __future__ import annotations

from fastapi_idempotency.types import (
    AcquireResult,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
)


class InMemoryStore:
    """Single-process store backed by a dict plus an ``asyncio.Lock``.

    Suitable for tests and single-worker deployments. Not safe across
    worker processes — use :class:`fastapi_idempotency.stores.redis.RedisStore`
    for anything multi-worker.

    Structurally conforms to :class:`fastapi_idempotency.store.Store`.
    """

    async def acquire(
        self,
        key: IdempotencyKey,
        fingerprint: Fingerprint,
        ttl: float,
    ) -> AcquireResult:
        raise NotImplementedError

    async def get(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        raise NotImplementedError

    async def complete(
        self,
        key: IdempotencyKey,
        response: CachedResponse,
        ttl: float,
    ) -> None:
        raise NotImplementedError

    async def release(self, key: IdempotencyKey) -> None:
        raise NotImplementedError
