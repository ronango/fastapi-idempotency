"""fastapi-idempotency: ``Idempotency-Key`` middleware for FastAPI / Starlette.

Public API is the names listed in ``__all__``. Store-implementation
types (``AcquireOutcome``, ``AcquireResult``, ``IdempotencyRecord``,
``IdempotencyState``, ``CachedResponse``, ``IdempotencyKey``,
``Fingerprint``) are importable for users writing custom ``Store``
implementations but kept out of ``__all__`` as advanced surface.

``RedisStore`` is loaded lazily via :pep:`562` ``__getattr__`` so
``import fastapi_idempotency`` works without the ``[redis]`` extra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    # Static-checker re-export — runtime import is lazy via __getattr__.
    from .stores.redis import RedisStore as RedisStore

__version__ = "0.2.0a1"

__all__ = [
    "ConflictError",
    "FingerprintMismatchError",
    "IdempotencyError",
    "IdempotencyMiddleware",
    "InMemoryStore",
    "RedisStore",
    "RequestTooLargeError",
    "Store",
    "StoreError",
    "__version__",
]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute access — defers redis-py until first use."""
    if name == "RedisStore":
        from .stores.redis import RedisStore  # noqa: PLC0415

        return RedisStore
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
