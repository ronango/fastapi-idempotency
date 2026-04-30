# Design decisions

This document records the reasoning behind non-obvious choices in `fastapi-idempotency`. Each section covers one decision: the context, the options considered, the choice, and the trade-offs accepted.

> Status: placeholder. Sections are filled in as each version lands; the tag under each heading marks when it becomes authoritative.

## Which response statuses are cacheable

_To be written during v0.2.0._

## Two-phase TTL for the `in_flight` state

**Status: v0.1.0.**

A record has two lifetime phases with separate TTLs:

- `in_flight_ttl` (default 30s): time the handler has to finish. A crashed or
  killed process can't run cleanup, so we rely on the short TTL to reclaim the
  slot — no separate crash-recovery mechanism in the store.
- `completed_ttl` (default 24h): time the cached response replays for once the
  handler succeeds. This is the window the client has to safely retry.

`Store.complete` replaces the IN_FLIGHT record with a COMPLETED one, resetting
`expires_at` to `now + completed_ttl`. `Store.release` removes the slot
outright — it's the explicit-cleanup path used when the handler raised or
returned a status we don't cache.

### Time source

All TTL math uses `time.time()` (wall-clock seconds since epoch). Alternatives
considered:

- `time.monotonic()`: safer against clock jumps within a process, but can't be
  compared across processes. The Redis backend (v0.2.0) will use wall-clock
  `EXPIRE` semantics, and having the in-memory store agree on units keeps
  cross-backend tests meaningful.
- Per-record `datetime`: heavier, no benefit at this scale.

The trade-off accepted: a wall-clock jump backwards could keep an expired slot
alive for longer than intended. In practice, servers are NTP-synced and the
window is milliseconds. Documented rather than engineered around.

### `release` is idempotent

Calling `release` on a key that doesn't exist (already expired, or never
acquired) is a no-op, not an error. Reason: the middleware's cleanup path on
a 5xx response races with `in_flight_ttl` expiry — if the TTL wins, the slot
is gone by the time `release` runs, and raising would crash the error-handling
path. Keeping `release` idempotent means the middleware doesn't need to guard
every call with `try/except`.

## Body fingerprint scope

**Status: v0.1.0.**

Fingerprint is computed over `method + path + query_string + body`,
length-prefixed and SHA-256 hashed. Each component carries
request-identity that the body alone doesn't.

### Why include each piece

- **Body**: the obvious one — same key with different payload should
  reject (`422 MISMATCH`). Without body in the fingerprint, an attacker
  who guessed someone's `Idempotency-Key` could submit a different body
  and get the original response replayed.
- **Path**: `POST /charges` and `POST /refunds` carrying the same body
  are different operations. A naive client might reuse a key across
  them; the path-level distinction prevents accidental cross-endpoint
  collision.
- **Method**: `POST /resource` and `DELETE /resource` are different
  intents. Including method makes the fingerprint match exactly one
  semantic operation.
- **Query string**: typically carries semantics (`?dry_run=true`,
  `?tenant=acme`, pagination cursors). Two requests differing only in
  query parameters are different operations and should not collide.

### Why length-prefix each component

Naive concatenation is ambiguous when component boundaries fall
anywhere in the byte stream. Body bytes are fully attacker-controlled
and can mimic the tail of any other component. For instance:

- `path="/orders/12"` + `body=b"3"`
- `path="/orders/123"` + `body=b""`

Both yield the byte stream `"/orders/123"` after concatenation —
identical hash, despite being different requests. Length-prefixing
each component before feeding it to the hasher makes the boundary
explicit, so prefix-aligned splits cannot collide.

(The same is true in theory for `method`/`path` boundaries, but in
practice HTTP methods are a fixed alphabet validated by the server
before middleware runs — that boundary isn't attacker-reachable.
Body is.)

### What is _not_ in the fingerprint

- **Headers** (other than `Idempotency-Key` itself): too volatile.
  `User-Agent`, `Accept-Language`, `X-Request-ID` would all change
  per-retry and break the matching guarantee. If header-based identity
  matters (e.g. tenant via `X-Tenant-Id`), use a `scope_factory`
  (planned for v0.3.0).
- **Time / nonce**: the whole point is determinism — two identical
  requests must produce the same fingerprint.
- **Source IP**: same reasoning as headers — too volatile and not
  intent-bearing.

## Streaming response pass-through

_To be written during v0.2.0._

## Why msgpack over JSON

_To be written during v0.1.0._

## Key scoping (`scope_factory`)

_To be written during v0.3.0._
