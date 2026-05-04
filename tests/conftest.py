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

Set ``REDIS_URL`` to bypass the testcontainer entirely and point at an
already-running Redis (e.g. a CI service container, or a local
host-network container on a host where Docker bridge networking is
broken). When ``REDIS_URL`` is set, Docker availability is irrelevant —
``redis_url`` yields the URL directly after a synchronous ``PING`` to
fail fast on a wrong/unreachable URL (same skip/fail semantics as the
container path; ``REDIS_TESTS_REQUIRED=1`` converts skip into fail).

DESTRUCTIVE: every redis-marked test runs ``FLUSHDB`` against the
selected database before the test body. Never point ``REDIS_URL`` at a
Redis you care about. As a defensive default, ``redis_client`` switches
to DB 15 when the URL has no ``/<db>`` component — most local Redis
deployments use DB 0 for Celery brokers, RQ queues, or app caches, so
DB 15 keeps the blast radius off the common path. Specify ``/<db>``
explicitly in ``REDIS_URL`` to override.

Container leaks: ``container.stop()`` runs in a ``finally`` block, but
SIGKILL/segfault can bypass it. Testcontainers' Ryuk sidecar reaps
orphaned containers within ~10s as a backstop. Don't disable Ryuk in CI
(``TESTCONTAINERS_RYUK_DISABLED`` should remain unset).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlparse

import pytest
import redis
import redis.asyncio
from testcontainers.redis import RedisContainer

# Pin the Redis image — alpine is ~5x smaller (faster CI pulls), and a
# version tag defends against ``latest`` drift. Patch-level images
# (7.2.x, alpine base CVE rebuilds) still float; pin a digest if a
# specific patch ever causes test divergence.
_REDIS_IMAGE = "redis:7.2-alpine"


def _redis_unavailable(reason: str) -> None:
    """Skip or fail per ``REDIS_TESTS_REQUIRED`` — shared by both URL paths."""
    if os.environ.get("REDIS_TESTS_REQUIRED") == "1":
        pytest.fail(f"REDIS_TESTS_REQUIRED=1 but {reason}")
    pytest.skip(
        f"{reason}; to bypass redis tests locally, run "
        f'`pytest -m "not redis"` or set REDIS_URL to an already-running Redis.',
    )


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """Boot a Redis container once per pytest session.

    Yields the container's URL as ``redis://host:port``. If ``REDIS_URL``
    is set in the environment, that URL is yielded directly (after a
    synchronous PING) and no container is started — the caller owns
    Redis lifecycle.
    """
    external = os.environ.get("REDIS_URL")
    if external:
        # Symmetry with the container path: validate connectivity once
        # at session start so a wrong URL surfaces as skip/fail per
        # REDIS_TESTS_REQUIRED, not per-test connection errors deeper in.
        try:
            sync_client = redis.Redis.from_url(external, socket_timeout=2.0)
            sync_client.ping()
            sync_client.close()
        except Exception as exc:
            _redis_unavailable(f"REDIS_URL={external!r} unreachable: {exc!r}")
        yield external
        return

    container = RedisContainer(_REDIS_IMAGE)
    try:
        container.start()
    except Exception as exc:
        # Broad on purpose: docker absent, daemon down, image pull fail —
        # all reduce to "skip these tests, run the rest".
        _redis_unavailable(f"Docker not available ({exc!r})")

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
    the same container instance. If the URL has no ``/<db>`` component,
    defaults to DB 15 — see the module docstring for the destructive-
    default rationale.
    """
    db_in_url = bool(urlparse(redis_url).path.lstrip("/"))
    client = (
        redis.asyncio.from_url(redis_url) if db_in_url else redis.asyncio.from_url(redis_url, db=15)
    )
    try:
        await client.flushdb()
        yield client
    finally:
        await client.aclose()
