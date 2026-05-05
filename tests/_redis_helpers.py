"""Shared redis-test helpers — single source of truth for client construction.

Both ``conftest.py``'s ``redis_client`` fixture and the spawn-worker in
``test_stores_redis_concurrent.py`` need to construct an async Redis
client with the same DB-defaulting rule. Centralizing here means a
future change (e.g. moving from DB 15 to a per-worker offset for
xdist) updates one place — without it, the subprocess silently flushes
a different DB than the parent and the cross-process test would pass
trivially against an empty key space.
"""

from __future__ import annotations

from urllib.parse import urlparse

import redis.asyncio


def make_async_client(redis_url: str) -> redis.asyncio.Redis:
    """Build an async Redis client; default DB to 15 if URL has no ``/<db>``.

    See ``conftest.py`` module docstring for the destructive-default
    rationale (FLUSHDB blast radius vs typical DB 0 usage).
    """
    db_in_url = bool(urlparse(redis_url).path.lstrip("/"))
    if db_in_url:
        return redis.asyncio.from_url(redis_url)
    return redis.asyncio.from_url(redis_url, db=15)
