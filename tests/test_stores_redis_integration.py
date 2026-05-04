"""Integration tests requiring a live Redis (via testcontainers).

Marked ``@pytest.mark.redis`` so they are skipped when Docker is
unavailable or when running ``pytest -m "not redis"`` (the existing CI
default).
"""

from __future__ import annotations

import pytest
import redis.asyncio


# TODO(slice-5): the cross-store conformance suite will exercise the same
# connectivity path as this smoke. Drop this file (or keep only as a
# fast-fail gate before the heavier suite) once slice 5 lands.
@pytest.mark.redis
async def test_redis_smoke_ping(redis_client: redis.asyncio.Redis) -> None:
    """Smoke: testcontainers Redis is reachable and responsive."""
    assert await redis_client.ping() is True
