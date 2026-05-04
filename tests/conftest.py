"""Shared pytest fixtures.

Redis fixtures spin up a real Redis instance via ``testcontainers`` for
tests marked ``@pytest.mark.redis``. The container is session-scoped
(one Docker startup per pytest run); ``redis_client`` is function-scoped
with a per-test ``FLUSHDB`` for isolation.

If Docker is not available, ``redis_url`` skips dependent tests with a
clear message rather than failing — local development without Docker is
supported. In CI, set ``REDIS_TESTS_REQUIRED=1`` to convert the skip
into a hard failure (slice 8 will export this in the redis job, so
"Docker missing in CI" doesn't masquerade as a green build).

Container leaks: ``container.stop()`` runs in a ``finally`` block, but
SIGKILL/segfault can bypass it. Testcontainers' Ryuk sidecar reaps
orphaned containers within ~10s as a backstop. Don't disable Ryuk in CI
(``TESTCONTAINERS_RYUK_DISABLED`` should remain unset).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import redis.asyncio
from testcontainers.redis import RedisContainer

# Pin the Redis image — alpine is ~5x smaller (faster CI pulls), and a
# version tag defends against ``latest`` drift. Patch-level images
# (7.2.x, alpine base CVE rebuilds) still float; pin a digest if a
# specific patch ever causes test divergence.
_REDIS_IMAGE = "redis:7.2-alpine"


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """Boot a Redis container once per pytest session.

    Yields the container's URL as ``redis://host:port``.
    """
    container = RedisContainer(_REDIS_IMAGE)
    try:
        container.start()
    except Exception as exc:
        # Broad on purpose: docker absent, daemon down, image pull fail —
        # all reduce to "skip these tests, run the rest".
        if os.environ.get("REDIS_TESTS_REQUIRED") == "1":
            pytest.fail(
                f"REDIS_TESTS_REQUIRED=1 but Redis container failed to start: {exc!r}",
            )
        pytest.skip(
            f"Docker not available ({exc!r}); to bypass redis tests "
            f'locally, run `pytest -m "not redis"`',
        )

    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}"
    finally:
        container.stop()


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[redis.asyncio.Redis]:
    """Async Redis client connected to the session-scoped container.

    ``FLUSHDB`` before each test ensures isolation between tests sharing
    the same container instance.
    """
    client = redis.asyncio.from_url(redis_url)
    try:
        await client.flushdb()
        yield client
    finally:
        await client.aclose()
