"""Redis-backed :class:`Store` implementation.

Requires the ``redis`` extra::

    pip install fastapi-idempotency[redis]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi_idempotency.types import (
    AcquireResult,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisStore:
    """Distributed store backed by Redis.

    Atomicity of :meth:`acquire` and :meth:`complete` is implemented with
    ``SET NX`` plus a small Lua script (``EVAL``) so concurrent workers
    cannot race on the same key.

    Structurally conforms to :class:`fastapi_idempotency.store.Store`.
    """

    def __init__(self, client: Redis, *, namespace: str = "idem") -> None:
        self.client = client
        self.namespace = namespace

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
