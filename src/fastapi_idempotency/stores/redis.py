"""Redis-backed :class:`Store` implementation.

Requires the ``redis`` extra::

    pip install fastapi-idempotency[redis]

Storage layout: one HASH per idempotency key at ``{namespace}:{key}`` with
fields ``fp`` (fingerprint hex string), ``state`` (enum value string), and
``data`` (msgpack-encoded :class:`IdempotencyRecord`). The Lua acquire
script reads ``fp`` and ``state`` directly without parsing msgpack,
keeping the server-side critical section minimal. ``HMGET`` reads all
three fields in one atomic snapshot, eliminating eviction-race holes
between sequential ``HGET``\\s.

TTL is set via ``PEXPIRE`` on the key; Redis handles eviction. See
``docs/DESIGN.md`` ("Time source for the Redis backend") for why
``created_at``/``expires_at`` inside the record are Python-clock while
TTL is Redis-clock.

Operators monitoring TTL should query Redis ``PTTL`` directly rather
than computing ``record.expires_at - time.time()`` — record fields are
advisory metadata, not the eviction authority.

The Lua script touches a single key, so it is safe under Redis Cluster
routing (no cross-slot operations). ``register_script`` caches the
script SHA1 locally; ``redis-py`` re-issues ``EVAL`` automatically on
``NOSCRIPT`` (e.g. after ``SCRIPT FLUSH`` or replica failover).

The caller owns the :class:`redis.asyncio.Redis` client lifecycle — we
do not close it. The client must be constructed with
``decode_responses=False`` (the default); the codec operates on raw
bytes and a ``decode_responses=True`` client will silently corrupt
msgpack payloads.

Recommended production client configuration::

    import redis.asyncio
    client = redis.asyncio.Redis(
        connection_pool=redis.asyncio.BlockingConnectionPool(
            max_connections=200,    # tune per RPS x p99 latency
            timeout=2.0,            # bound queue wait on saturation
        ),
        socket_timeout=1.0,         # bound stalled-Redis cascades
        socket_connect_timeout=1.0,
    )

Without ``socket_timeout`` an unhealthy Redis blocks ``acquire``
indefinitely; without ``BlockingConnectionPool`` a stalled Redis
exhausts the default 50-connection pool and surfaces as raw
``ConnectionError``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

try:
    import redis.asyncio  # noqa: F401
    from redis.exceptions import RedisError
except ImportError as exc:  # pragma: no cover
    msg = (
        "RedisStore requires the 'redis' extra. "
        "Install with: pip install fastapi-idempotency[redis]"
    )
    raise ImportError(msg) from exc

from fastapi_idempotency.errors import StoreError
from fastapi_idempotency.stores._serde import decode_record, encode_record
from fastapi_idempotency.types import (
    AcquireOutcome,
    AcquireResult,
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis


# Atomic acquire as a single Lua call — classifies into one of four
# AcquireOutcome branches in one round-trip. Returns a 2-element table:
# the outcome value (matches AcquireOutcome.<m>.value strings) and the
# record's msgpack bytes (newly inserted or existing, depending on branch).
#
# HMGET reads all three fields in one atomic snapshot — closes the
# eviction-race window where sequential HGETs could see a partially
# evicted key (state=False, falling through to REPLAY with garbage data).
#
#   KEYS: [1] full namespaced key
#   ARGV: [1] fingerprint hex, [2] ttl_ms (positive int), [3] new-record msgpack bytes
_ACQUIRE_LUA = """
local key = KEYS[1]
local fp = ARGV[1]
local ttl_ms = tonumber(ARGV[2])
local data = ARGV[3]

if not ttl_ms or ttl_ms <= 0 then
  return redis.error_reply('ttl_ms must be a positive integer')
end

local existing = redis.call('HMGET', key, 'fp', 'state', 'data')
local existing_fp = existing[1]
local existing_state = existing[2]
local existing_data = existing[3]

if not existing_fp then
  redis.call('HSET', key, 'fp', fp, 'state', 'in_flight', 'data', data)
  redis.call('PEXPIRE', key, ttl_ms)
  return {'created', data}
end

if existing_fp ~= fp then
  return {'mismatch', existing_data}
end

if existing_state == 'in_flight' then
  return {'in_flight', existing_data}
end

return {'replay', existing_data}
"""


class RedisStore:  # pragma: no cover
    """Distributed store backed by Redis.

    Atomic ``acquire`` is implemented as a single Lua ``EVAL`` so concurrent
    workers cannot race on the same key. ``get`` / ``complete`` / ``release``
    arrive in the next slice.

    Structurally conforms to :class:`fastapi_idempotency.store.Store`.

    .. note::
        Implementation in progress (issue #17). Methods other than
        ``acquire`` raise :class:`NotImplementedError` until the next
        slice. ``# pragma: no cover`` stays on until the cross-store
        conformance suite (slice 5) exercises this class against a real
        Redis via testcontainers.
    """

    def __init__(self, client: Redis, *, namespace: str = "idem") -> None:
        # redis-py's get_encoder() is untyped in stubs as of 5.x.
        encoder = client.get_encoder()  # type: ignore[no-untyped-call]
        if encoder.decode_responses:
            msg = (
                "RedisStore requires a client with decode_responses=False; "
                "the codec operates on raw bytes."
            )
            raise ValueError(msg)
        self.client = client
        self.namespace = namespace
        self._acquire_script: Any = client.register_script(_ACQUIRE_LUA)

    async def acquire(
        self,
        key: IdempotencyKey,
        fingerprint: Fingerprint,
        ttl: float,
    ) -> AcquireResult:
        # Build the record we'd insert if CREATED. For other branches the
        # Lua script returns the existing record's bytes and this serialized
        # blob is discarded — accepted simplicity-tax for a single-script API.
        now = time.time()
        new_record = IdempotencyRecord(
            key=key,
            fingerprint=fingerprint,
            state=IdempotencyState.IN_FLIGHT,
            created_at=now,
            expires_at=now + ttl,
            response=None,
        )
        new_data = encode_record(new_record)

        full_key = self._key(key)
        ttl_ms = int(ttl * 1000)

        try:
            result = await self._acquire_script(
                keys=[full_key],
                args=[str(fingerprint), ttl_ms, new_data],
            )
        except (RedisError, OSError) as exc:
            msg = f"Redis acquire failed: {repr(exc)[:200]}"
            raise StoreError(msg) from exc

        outcome_raw, record_bytes = result
        outcome_value = (
            outcome_raw.decode("ascii")
            if isinstance(outcome_raw, (bytes, bytearray))
            else outcome_raw
        )
        outcome = AcquireOutcome(outcome_value)
        record = decode_record(record_bytes)

        return AcquireResult(outcome=outcome, record=record)

    async def get(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        raise NotImplementedError

    async def complete(
        self,
        key: IdempotencyKey,
        response: CachedResponse,
        ttl: float,
    ) -> None:
        raise NotImplementedError

    async def release(self, key: IdempotencyKey) -> None:
        raise NotImplementedError

    def _key(self, key: IdempotencyKey) -> str:
        return f"{self.namespace}:{key}"
