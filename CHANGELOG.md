# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
