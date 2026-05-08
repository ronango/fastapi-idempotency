"""Redis-backed :class:`Store` implementation.

Requires the ``redis`` extra (``pip install fastapi-idempotency[redis]``).

Storage layout: one HASH per idempotency key at ``{namespace}:{key}``
with fields ``fp`` / ``state`` / ``data`` (msgpack-encoded
:class:`IdempotencyRecord`). TTL is server-side via ``PEXPIRE``.
The Lua acquire script reads ``fp`` and ``state`` directly without
parsing msgpack, keeping the critical section minimal.

The client must use ``decode_responses=False`` (the default); the
codec operates on raw bytes. The caller owns client lifecycle. Tune
``socket_timeout`` and use ``BlockingConnectionPool`` for production
— see the Redis quickstart in ``README.md`` for a worked example.

See ``docs/DESIGN.md`` ("Time source for the Redis backend") for the
TTL/clock split and cluster-routing notes.
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
    """Wrap a redis-py exception as :class:`StoreError`."""
    # Truncated repr defends against log-volume DoS via attacker-crafted
    # multi-MB exception text.
    logger.debug("redis %s failed: %r", op, exc)
    msg = f"Redis {op} failed: {repr(exc)[:200]}"
    return StoreError(msg)


# Single HMGET (not sequential HGETs) snapshots fp/state/data in one
# atomic read so a concurrent HSET can't interleave between fields.
#
#   KEYS: [1] full namespaced key
#   ARGV: [1] fingerprint hex, [2] ttl_ms (positive int), [3] new-record msgpack bytes
#   Returns: 2-element table {outcome_string, record_msgpack}
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


#   KEYS: [1] full namespaced key
#   ARGV: [1] new-record msgpack bytes, [2] ttl_ms (positive int)
#   Returns: 'ok', or error_reply if the slot was evicted between
#   Python's read and this call.
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


class RedisStore:
    """Distributed store backed by Redis.

    Atomic ``acquire`` is implemented as a single Lua ``EVAL`` so concurrent
    workers cannot race on the same key. ``get`` / ``complete`` / ``release``
    are straightforward HSET/HGET/DEL paths — only ``acquire`` needs Lua.

    Structurally conforms to :class:`fastapi_idempotency.store.Store`.
    """

    def __init__(self, client: Redis, *, namespace: str = "idem") -> None:
        encoder = client.get_encoder()  # type: ignore[no-untyped-call]
        if encoder.decode_responses:
            msg = (
                "RedisStore requires a client with decode_responses=False; "
                "the codec operates on raw bytes."
            )
            raise ValueError(msg)
        self.client = client
        self.namespace = namespace
        # ``register_script`` caches the SHA1 locally; redis-py re-issues
        # ``EVAL`` automatically on ``NOSCRIPT`` (replica failover, SCRIPT FLUSH).
        self._acquire_script: Any = client.register_script(_ACQUIRE_LUA)
        self._complete_script: Any = client.register_script(_COMPLETE_LUA)

    async def acquire(
        self,
        key: IdempotencyKey,
        fingerprint: Fingerprint,
        ttl: float,
    ) -> AcquireResult:
        # Pre-encode the would-be-CREATED record; non-CREATED branches
        # discard it. Single-script trade-off.
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
        # Defense in depth: catch the sub-millisecond race between
        # PEXPIRE and HGET via Python clock.
        if record.is_expired(time.time()):
            return None
        return record

    async def complete(
        self,
        key: IdempotencyKey,
        response: CachedResponse,
        ttl: float,
    ) -> None:
        # 2-RTT (HGET + Lua): a TTL eviction + re-acquire between them
        # would let us complete-overwrite a fresh slot's data. Bounded
        # by in_flight_ttl tuning; harden in a future pass.
        full_key = self._key(key)

        # Read to preserve identity (key, fingerprint, created_at).
        try:
            existing_data = await self.client.hget(full_key, "data")  # type: ignore[misc]
        except (RedisError, OSError) as exc:
            raise _wrap_redis_error("complete", exc) from exc

        if existing_data is None:
            msg = f"cannot complete unknown idempotency key: {key!r}"
            raise StoreError(msg)

        existing = decode_record(existing_data)
        # Same Python-clock guard as ``get``: PEXPIRE may not have
        # fired yet on a sub-millisecond race.
        now = time.time()
        if existing.is_expired(now):
            msg = f"cannot complete unknown idempotency key: {key!r}"
            raise StoreError(msg)

        new_record = replace(
            existing,
            state=IdempotencyState.COMPLETED,
            expires_at=now + ttl,
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
