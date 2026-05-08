# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Streaming response pass-through (#18). FastAPI / Starlette
  `StreamingResponse` (and any ASGI app emitting `more_body=True` on
  the first body chunk) now flows through the middleware live instead
  of being buffered into memory. Streamed responses carry an
  `Idempotency-Stored: false` header so retries know they won't replay,
  and the in-flight slot is released — a stream cannot be cached.
  Detection defers `http.response.start` until the first body chunk
  so the header can be injected before start hits the wire.
  Non-streaming responses keep the v0.1.0 caching/replay behavior
  unchanged. The `Idempotency-Stored: false` header now carries a
  union meaning ("response will not replay on retry"): emitted both
  for streaming pass-through and for the v0.1.0 storage-failure path.
- `RedisStore` — distributed `Store` backend backed by Redis (#17).
  Atomic `acquire` via a single Lua `EVAL` so concurrent workers cannot
  double-claim a key; `get` / `complete` / `release` are straightforward
  HSET/HGET/DEL paths. Requires the new `[redis]` extra
  (`pip install fastapi-idempotency[redis]`); `RedisStore` is exposed
  lazily on the package root so the bare install path stays free of
  the redis-py dependency. Defense-in-depth Python-side `is_expired`
  filter on `get` and `complete` covers the sub-millisecond race
  between `PEXPIRE` and `HGET`.
- msgpack record codec (`stores._serde`) with a v1 envelope, schema
  versioning, and bounded payload size — used by `RedisStore` to
  serialize `IdempotencyRecord` to bytes.
- Cross-store conformance suite (`tests/test_stores_conformance.py`) —
  parametrized over both backends so any Store-protocol drift between
  `InMemoryStore` and `RedisStore` (or future implementations) is
  caught by one test run. Covers acquire/get/complete/release
  classification, identity preservation, state-machine edges, single-
  process concurrency (`asyncio.gather` of 10 coros), and TTL expiry
  under `asyncio.sleep`. Slow tests carry `@pytest.mark.slow`; redis
  variants carry `@pytest.mark.redis`.
- Cross-process concurrent-acquire test
  (`tests/test_stores_redis_concurrent.py`): N OS processes via
  `ProcessPoolExecutor(spawn)` with a `Manager().Barrier` so all
  workers race simultaneously — the distributed-locking property that
  justifies the Redis dependency.
- `REDIS_URL` env var in `tests/conftest.py` bypasses testcontainers
  for CI service containers and broken-Docker-bridge hosts; defaults
  to DB 15 to keep `FLUSHDB` blast radius off operators' DB 0.
- `REDIS_TESTS_REQUIRED=1` flips the redis-tests-skipped path into a
  hard fail (used by the new `check-redis` CI job).

### Changed

- CI matrix now runs the full test suite (including `@pytest.mark.redis`
  and `@pytest.mark.slow`) against a Redis service container on every
  Python version (3.10–3.13). The 95% coverage gate fires on every
  matrix job — RedisStore parity bugs specific to a single Python
  version get caught at PR time rather than at upgrade time.

### Fixed

- The internal request-body replay no longer raises `RuntimeError` on
  the second `receive()` call. Subsequent calls now forward to the
  original ASGI `receive`, so apps that listen for `http.disconnect`
  during streaming (e.g. Starlette's `StreamingResponse` background
  task) work correctly (#18).
- `InMemoryStore.complete` now raises `StoreError` on expired records
  (matching `RedisStore` and the `Store` protocol contract). Previously
  it silently overwrote the expired in-flight slot with a fresh
  `COMPLETED` record, masking the `in_flight_ttl` tuning signal
  operators are supposed to see (#17).

## [0.1.0] - 2026-04-30

### Added

- `IdempotencyMiddleware` — ASGI middleware enforcing `Idempotency-Key`
  semantics on POST/PATCH/PUT/DELETE.
- `Store` Protocol — pluggable storage contract.
- `InMemoryStore` — single-process backend (asyncio.Lock + dict).
- Body-fingerprint detection of key reuse with altered payloads
  (responds 422).
- Two-phase TTL: short `in_flight_ttl` (default 30s) + long
  `completed_ttl` (default 24h).
- Replay path with `Idempotent-Replayed: true` response header.
- 5xx and exception responses release the slot for retry.
- `Idempotency-Stored: false` header when storage fails after the
  handler succeeds.
- `max_body_bytes` pre-check via `Content-Length` to skip idempotency
  on oversized bodies.
- Domain exceptions: `IdempotencyError`, `ConflictError`,
  `FingerprintMismatchError`, `StoreError`, `RequestTooLargeError`.

### Known limitations

- `InMemoryStore` is single-process only — multi-worker deployments
  need a distributed backend.
- `RedisStore` exists as a stub; real implementation lands in v0.2.0.
- Streaming response pass-through deferred to v0.2.0 (responses are
  fully buffered in memory).
- No `scope_factory` / per-user scoping yet — planned for v0.3.0.
- No HMAC fingerprint; deterministic SHA-256 — same key + same body
  observable across requests in the same scope.
