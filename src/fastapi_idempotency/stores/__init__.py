"""Concrete Store implementations.

:class:`~fastapi_idempotency.stores.redis.RedisStore` is intentionally not
re-exported from here: importing it requires the optional ``redis`` extra.
Import it explicitly from :mod:`fastapi_idempotency.stores.redis` when you
need it.
"""

from __future__ import annotations

from .memory import InMemoryStore

__all__ = ["InMemoryStore"]
