"""fastapi-idempotency: ``Idempotency-Key`` middleware for FastAPI / Starlette.

Public API (the names listed in ``__all__``) is what most users need:
the middleware, the in-memory store, ``RedisStore`` (requires the
``[redis]`` extra), the ``Store`` Protocol for custom backends, and
the exception hierarchy for catch blocks.

Store-implementation details (``AcquireOutcome``, ``AcquireResult``,
``IdempotencyRecord``, ``IdempotencyState``, ``CachedResponse``,
``IdempotencyKey``, ``Fingerprint``) are still importable from this
module for users writing custom ``Store`` implementations, but are kept
out of ``__all__`` to signal that they are advanced surface — not what
a typical caller reaches for.

``RedisStore`` is loaded lazily via :pep:`562` ``__getattr__`` so that
``import fastapi_idempotency`` keeps working without the ``[redis]``
extra installed — accessing ``fastapi_idempotency.RedisStore`` is what
triggers the import (and the helpful ``ImportError`` from
``stores.redis`` if the extra is missing).
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
    # Static-checker re-export so ``from fastapi_idempotency import RedisStore``
    # type-checks without forcing an import at module load (which would fail
    # for users who installed the package without the ``[redis]`` extra).
    from .stores.redis import RedisStore as RedisStore

__version__ = "0.1.0"

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
    """PEP 562 lazy attribute access.

    Defers importing ``RedisStore`` (and the optional ``redis`` extra
    it depends on) until the user actually references it. Without this,
    ``import fastapi_idempotency`` would fail on installations that
    lack the ``[redis]`` extra — even for users who only use
    ``InMemoryStore``.
    """
    if name == "RedisStore":
        # Lazy import is the whole point: deferring redis-py until access
        # lets users without the [redis] extra still `import fastapi_idempotency`.
        from .stores.redis import RedisStore  # noqa: PLC0415

        return RedisStore
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
