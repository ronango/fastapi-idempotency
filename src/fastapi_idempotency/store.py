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
    the only method that must be atomic: it is the single compare-and-set
    that classifies the request into one of the four
    :class:`AcquireOutcome` branches. ``get``, ``complete``, and
    ``release`` may be straightforward reads/writes, but implementations
    are still responsible for any locking their data structure needs
    (e.g. an in-memory dict requires an ``asyncio.Lock``).

    Expiry is time-based: every record carries ``expires_at`` and stores
    treat records whose ``expires_at`` has elapsed as absent. Stores use
    their own clock (``time.time`` in the in-memory backend, ``EXPIRE``
    in Redis); no real-time precision is promised.
    """

    async def acquire(
        self,
        key: IdempotencyKey,
        fingerprint: Fingerprint,
        ttl: float,
    ) -> AcquireResult:
        """Atomically claim or inspect an idempotency slot.

        Classification rules (all checked inside the atomic section):

        - No record, or the existing record has expired → insert a fresh
          IN_FLIGHT record and return ``CREATED``. The caller now owns
          the slot and must eventually call ``complete`` or ``release``.
        - Existing, unexpired record with a different fingerprint →
          ``MISMATCH``. The caller should respond 422; the stored
          response (if any) is returned for diagnostic purposes but not
          meant to be replayed.
        - Existing, unexpired record with the same fingerprint and state
          ``IN_FLIGHT`` → ``IN_FLIGHT``. The caller should respond 409.
        - Existing, unexpired record with the same fingerprint and state
          ``COMPLETED`` → ``REPLAY``. The caller replays
          ``record.response`` verbatim (the ``Idempotent-Replayed``
          header is added by the middleware, not the store).

        Fingerprint comparison happens inside the atomic section so no
        TOCTOU race can classify the request twice differently.
        """
        ...

    async def get(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        """Fetch a record by key, or return ``None`` if absent or expired.

        A non-mutating read: expired records are not deleted as a side
        effect of ``get``. Cleanup happens lazily inside ``acquire`` when
        the slot is reclaimed.
        """
        ...

    async def complete(
        self,
        key: IdempotencyKey,
        response: CachedResponse,
        ttl: float,
    ) -> None:
        """Transition IN_FLIGHT → COMPLETED, persisting ``response`` for ``ttl`` seconds.

        Preserves ``key``, ``fingerprint``, and ``created_at`` from the
        in-flight record; overwrites ``state``, ``expires_at``, and
        ``response``. The new ``expires_at`` is ``now + ttl`` (this
        ``ttl`` *replaces* any remaining acquire-phase TTL — eviction
        is reseated, not extended).

        The caller must own the slot (i.e. previously received
        ``CREATED`` from ``acquire``). If no record exists for ``key``
        — or the existing record has expired — the store raises
        :class:`StoreError`. This typically means the in-flight TTL
        elapsed before the handler finished; tune ``in_flight_ttl``
        upward if this fires in production.
        """
        ...

    async def release(self, key: IdempotencyKey) -> None:
        """Remove the slot for ``key``; no-op if no record exists.

        Idempotent by design so the middleware's error path can call it
        unconditionally — it races with ``in_flight_ttl`` expiry, and
        raising on a missing record would crash the error handler.

        Process crashes never call ``release``; orphaned slots are
        recovered passively via the in-flight TTL. Stores need no
        separate recovery mechanism.
        """
        ...
