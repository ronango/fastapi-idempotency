"""fastapi-idempotency: ``Idempotency-Key`` middleware for FastAPI / Starlette.

Public API (the names listed in ``__all__``) is what most users need:
the middleware, the in-memory store, the ``Store`` Protocol for custom
backends, and the exception hierarchy for catch blocks.

Store-implementation details (``AcquireOutcome``, ``AcquireResult``,
``IdempotencyRecord``, ``IdempotencyState``, ``CachedResponse``,
``IdempotencyKey``, ``Fingerprint``) are still importable from this
module for users writing custom ``Store`` implementations, but are kept
out of ``__all__`` to signal that they are advanced surface — not what
a typical caller reaches for.
"""

from __future__ import annotations

from .errors import (
    ConflictError,
    FingerprintMismatchError,
    IdempotencyError,
    RequestTooLargeError,
    StoreError,
)
from .middleware import IdempotencyMiddleware
from .store import Store
from .stores.memory import InMemoryStore
from .types import (
    AcquireOutcome as AcquireOutcome,
    AcquireResult as AcquireResult,
    CachedResponse as CachedResponse,
    Fingerprint as Fingerprint,
    IdempotencyKey as IdempotencyKey,
    IdempotencyRecord as IdempotencyRecord,
    IdempotencyState as IdempotencyState,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "ConflictError",
    "FingerprintMismatchError",
    "IdempotencyError",
    "IdempotencyMiddleware",
    "InMemoryStore",
    "RequestTooLargeError",
    "Store",
    "StoreError",
    "__version__",
]
