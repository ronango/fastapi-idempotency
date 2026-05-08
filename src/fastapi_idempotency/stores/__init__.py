"""Concrete Store implementations.

``RedisStore`` is not re-exported here — it requires the optional
``redis`` extra. Use ``from fastapi_idempotency import RedisStore`` (lazy
via PEP 562) or import directly from ``fastapi_idempotency.stores.redis``.
"""

from __future__ import annotations

from .memory import InMemoryStore

__all__ = ["InMemoryStore"]
