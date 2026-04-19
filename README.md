# fastapi-idempotency

> Opinionated `Idempotency-Key` middleware for FastAPI / Starlette.
> Implements the IETF draft spec with explicit handling of concurrent retries, body-fingerprint mismatch, and failed requests.
> Pluggable backends (in-memory, Redis), `mypy --strict`, targets 95%+ test coverage.

## Status

**Pre-release / experimental.** The public API is not stable; expect breaking changes before v0.2.0. Do not use in production yet.

## Roadmap

- **v0.1.0** — minimal working version: in-memory store, middleware core, fingerprint, two-phase TTL, replay path, basic error handling. Publish to TestPyPI.
- **v0.2.0** — production-ready: Redis backend, `409 Conflict` on concurrent requests, `422` on body mismatch, streaming pass-through, full CI matrix (3.10–3.13), PyPI release via Trusted Publisher.
- **v0.3.0** — ergonomics: lifecycle hooks (`on_replay`, `on_conflict`, `on_mismatch`), per-route config, per-user scoping, benchmarks.
- **v0.4.0** — polish: full docs site, cookbook (payments, webhooks), release-notes automation.
