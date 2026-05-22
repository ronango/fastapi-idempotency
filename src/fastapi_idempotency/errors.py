"""Exception hierarchy raised by the middleware and stores."""

from __future__ import annotations


class IdempotencyError(Exception):
    """Base class for every error raised by this package."""


class StoreError(IdempotencyError):
    """The backing store failed (network, serialization, protocol error)."""


class RequestTooLargeError(IdempotencyError):
    """Request body exceeded the configured ``max_bytes`` while buffering."""
