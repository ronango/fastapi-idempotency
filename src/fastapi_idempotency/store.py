"""Storage backend contract."""

from __future__ import annotations

from typing import Protocol

from .types import (
    AcquireResult,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
)


class Store(Protocol):
    """Pluggable storage backend for idempotency records.

    Implementations must be safe under concurrent access. ``acquire`` is
    the only method that must be atomic: it performs the compare-and-set
    that decides which of the four :class:`AcquireOutcome` branches the
    caller takes. The remaining methods may be straightforward reads/writes.
    """

    async def acquire(
        self,
        key: IdempotencyKey,
        fingerprint: Fingerprint,
        ttl: float,
    ) -> AcquireResult:
        """Atomically claim or inspect an idempotency slot.

        The returned :class:`AcquireResult` tells the caller what to do
        next: implement the request (``CREATED``), respond 409
        (``IN_FLIGHT``), replay the stored response (``REPLAY``), or
        respond 422 (``MISMATCH``). Fingerprint comparison is the store's
        responsibility to avoid a TOCTOU race between read and compare.
        """
        ...

    async def get(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        """Fetch a record by key, or return ``None`` if absent or expired."""
        ...

    async def complete(
        self,
        key: IdempotencyKey,
        response: CachedResponse,
        ttl: float,
    ) -> None:
        """Transition IN_FLIGHT → COMPLETED and persist the response for ``ttl`` seconds."""
        ...

    async def release(self, key: IdempotencyKey) -> None:
        """Remove an IN_FLIGHT slot after the handler raised before completing.

        This is the explicit-cleanup path. Process crashes are handled
        passively by ``in_flight_ttl`` expiring the orphaned slot — stores
        do not need a separate recovery mechanism.
        """
        ...
