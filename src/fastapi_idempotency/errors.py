"""Exception hierarchy raised by the middleware and stores."""

from __future__ import annotations


class IdempotencyError(Exception):
    """Base class for every error raised by this package."""


class ConflictError(IdempotencyError):
    """A concurrent request already holds the in-flight slot for this key."""


class FingerprintMismatchError(IdempotencyError):
    """Same Idempotency-Key was reused with a different request body."""


class StoreError(IdempotencyError):
    """The backing store failed (network, serialization, protocol error)."""
