"""fastapi-idempotency: ``Idempotency-Key`` middleware for FastAPI / Starlette."""

from __future__ import annotations

from .errors import (
    ConflictError,
    FingerprintMismatchError,
    IdempotencyError,
    RequestTooLargeError,
    StoreError,
)
from .middleware import IdempotencyMiddleware, ScopeFactory
from .store import Store
from .stores.memory import InMemoryStore
from .types import (
    AcquireOutcome,
    AcquireResult,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "AcquireOutcome",
    "AcquireResult",
    "CachedResponse",
    "ConflictError",
    "Fingerprint",
    "FingerprintMismatchError",
    "IdempotencyError",
    "IdempotencyKey",
    "IdempotencyMiddleware",
    "IdempotencyRecord",
    "IdempotencyState",
    "InMemoryStore",
    "RequestTooLargeError",
    "ScopeFactory",
    "Store",
    "StoreError",
    "__version__",
]
