"""Public type definitions shared across the package."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import NewType, TypeAlias

from starlette.requests import Request

IdempotencyKey = NewType("IdempotencyKey", str)
Fingerprint = NewType("Fingerprint", str)
ScopeFactory: TypeAlias = Callable[[Request], str | Awaitable[str]]


class IdempotencyState(str, Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"


class AcquireOutcome(str, Enum):
    """What happened when the middleware asked the store for a slot.

    - ``CREATED``: the caller now owns the IN_FLIGHT slot and must
      eventually call ``complete`` or ``release``.
    - ``IN_FLIGHT``: another worker already holds the slot — respond 409.
    - ``REPLAY``: a COMPLETED record exists — replay ``record.response``.
    - ``MISMATCH``: the key exists with a different fingerprint — respond 422.
    """

    CREATED = "created"
    IN_FLIGHT = "in_flight"
    REPLAY = "replay"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class CachedResponse:
    status_code: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    media_type: str | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    key: IdempotencyKey
    fingerprint: Fingerprint
    state: IdempotencyState
    created_at: float
    expires_at: float
    response: CachedResponse | None = None


@dataclass(frozen=True, slots=True)
class AcquireResult:
    """Outcome of :meth:`Store.acquire` plus the relevant record.

    The meaning of ``record`` depends on ``outcome``:

    - ``CREATED``: the newly-inserted IN_FLIGHT record owned by the caller.
    - ``IN_FLIGHT``: the existing IN_FLIGHT record held by another worker.
    - ``REPLAY``: the COMPLETED record whose ``response`` must be replayed.
    - ``MISMATCH``: the existing record whose fingerprint disagrees with the
      caller's; ``response`` may or may not be populated.
    """

    outcome: AcquireOutcome
    record: IdempotencyRecord
