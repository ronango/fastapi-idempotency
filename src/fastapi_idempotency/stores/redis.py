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

import logging
import time
from dataclasses import replace
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


logger = logging.getLogger(__name__)


def _wrap_redis_error(op: str, exc: BaseException) -> StoreError:
    """Wrap a redis-py exception as :class:`StoreError`.

    Logs at DEBUG so operators flipping to debug level get a breadcrumb
    before the StoreError bubbles up to the middleware. Truncated repr
    defends against log-volume DoS via attacker-crafted multi-MB exception
    text.
    """
    logger.debug("redis %s failed: %r", op, exc)
    msg = f"Redis {op} failed: {repr(exc)[:200]}"
    return StoreError(msg)


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


# Atomic complete: validates the slot still exists (TTL race may have
# evicted it between Python's read and this script), then writes the new
# state + data and refreshes TTL to ``completed_ttl``. Returns 'ok' on
# success, ``redis.error_reply`` if the slot is gone.
#
#   KEYS: [1] full namespaced key
#   ARGV: [1] new-record msgpack bytes, [2] ttl_ms (positive int)
_COMPLETE_LUA = """
local key = KEYS[1]
local data = ARGV[1]
local ttl_ms = tonumber(ARGV[2])

if not ttl_ms or ttl_ms <= 0 then
  return redis.error_reply('ttl_ms must be a positive integer')
end

if redis.call('EXISTS', key) == 0 then
  return redis.error_reply('cannot complete unknown idempotency key')
end

redis.call('HSET', key, 'state', 'completed', 'data', data)
redis.call('PEXPIRE', key, ttl_ms)
return 'ok'
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
        self._complete_script: Any = client.register_script(_COMPLETE_LUA)

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
            raise _wrap_redis_error("acquire", exc) from exc

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
        full_key = self._key(key)
        try:
            # redis-py 5.x async-client typing is imprecise; runtime is awaitable.
            data = await self.client.hget(full_key, "data")  # type: ignore[misc]
        except (RedisError, OSError) as exc:
            raise _wrap_redis_error("get", exc) from exc

        if data is None:
            return None

        record = decode_record(data)
        # Symmetry with InMemoryStore: filter expired records Python-side.
        # Defense in depth — Redis PEXPIRE is the primary eviction path,
        # but a millisecond-scale race between expiry and our read can
        # surface a record that is already expired by client-side clock.
        if record.is_expired(time.time()):
            return None
        return record

    async def complete(
        self,
        key: IdempotencyKey,
        response: CachedResponse,
        ttl: float,
    ) -> None:
        # NOTE: this is a 2-RTT operation (HGET + Lua). Between the read and
        # the script call, a TTL eviction + re-acquire could create a fresh
        # slot at the same key — we'd then complete-overwrite that fresh
        # slot's data. Matches InMemoryStore behavior (caller-owns-slot
        # contract; race bounded by in_flight_ttl tuning). Tracked for both
        # backends in a v0.3.0+ hardening pass.
        full_key = self._key(key)

        # Read existing to preserve identity (key, fingerprint, created_at).
        try:
            existing_data = await self.client.hget(full_key, "data")  # type: ignore[misc]
        except (RedisError, OSError) as exc:
            raise _wrap_redis_error("complete", exc) from exc

        if existing_data is None:
            msg = f"cannot complete unknown idempotency key: {key!r}"
            raise StoreError(msg)

        existing = decode_record(existing_data)
        new_record = replace(
            existing,
            state=IdempotencyState.COMPLETED,
            expires_at=time.time() + ttl,
            response=response,
        )
        new_data = encode_record(new_record)

        ttl_ms = int(ttl * 1000)
        try:
            await self._complete_script(
                keys=[full_key],
                args=[new_data, ttl_ms],
            )
        except (RedisError, OSError) as exc:
            raise _wrap_redis_error("complete", exc) from exc

    async def release(self, key: IdempotencyKey) -> None:
        full_key = self._key(key)
        try:
            await self.client.delete(full_key)
        except (RedisError, OSError) as exc:
            raise _wrap_redis_error("release", exc) from exc

    def _key(self, key: IdempotencyKey) -> str:
        return f"{self.namespace}:{key}"
