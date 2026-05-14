# Design decisions

This document records the reasoning behind non-obvious choices in `fastapi-idempotency`. Each section covers one decision: the context, the options considered, the choice, and the trade-offs accepted.

> Each section is tagged with the version in which it became authoritative. Sections tagged "planned for vN" describe decisions ratified but not yet implemented.

## Which response statuses are cacheable

**Status: v0.2.0.**

- **2xx, 3xx, 4xx** are cached — they reflect a deterministic handler
  outcome that a retry would reach again. The replay path emits the
  same body and headers (minus the volatile-header denylist).
  - **3xx redirect caveat**: a `Location` header carrying a presigned
    URL, OAuth `state` parameter, or other single-use token will be
    re-emitted byte-for-byte on every replay within `completed_ttl`.
    The right shape is to return a stable redirect target and let the
    token-mint sit behind it; if a single-use redirect really is the
    response, the client should omit `Idempotency-Key` on that route,
    or the route should be excluded from the middleware (per-route
    config lands in v0.3.0). Adding `Location` to the volatile
    denylist would silently break the common case of stable
    redirects, so the trade-off lands on the handler.
- **5xx is *not* cached.** A 5xx is treated as a transient failure:
  the slot is released and a retry runs the handler fresh. Caching
  it would freeze the failure into every retry for `completed_ttl`,
  which is precisely the wrong behaviour. The threshold lives in
  `IdempotencyMiddleware.FIRST_SERVER_ERROR_STATUS` (= 500).
- **Streaming responses are not cached** (see "Streaming response
  pass-through" below). They're forwarded live with
  `Idempotency-Stored: false` so a client knows the slot won't replay.
- **Synthesized middleware responses (400, 409, 413, 422)** are not
  cached because they are produced by the middleware itself, not by
  the wrapped handler — there is nothing to replay.

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

## Time source for the Redis backend

**Status: v0.2.0.**

`RedisStore` mixes two clocks deliberately:

- **TTL eviction** is server-side: `PEXPIRE` on the Redis key gives Redis
  full control over when the record disappears. No Python clock involvement
  in eviction means workers can't disagree on "is this slot still alive?"
  during a brownout.
- **Record fields** (`created_at`, `expires_at` inside
  `IdempotencyRecord`) are computed in Python via `time.time()` before
  encoding.

### Why not use `redis.call('TIME')` for record fields too

The Lua acquire script can read Redis server time via `TIME`, and an
earlier draft of v0.2.0 planned to inject server timestamps into every
new record. The implementation revealed the cost: for Lua to set
`created_at`/`expires_at` _inside_ the msgpack-encoded record, one of:

1. Re-encode the record server-side via `cmsgpack` — but Lua's msgpack
   library and Python's `msgpack` library disagree on float encoding
   (subnormal handling, NaN representation). Cross-language drift would
   break round-trip determinism (which we lock down via a test in
   `_serde.py`).
2. Store timestamps as separate Hash fields outside the msgpack blob,
   and splice them into the decoded record Python-side. Significant
   `_serde.py` changes for a marginal benefit.

The benefit at issue: clock drift between workers. On NTP-synced hosts
the drift is milliseconds; v0.2.0's smallest TTL default is 30 s
(in-flight), 24 h (completed). A ms-scale skew on a 30 s TTL is 0.03 %.

**Trade-off accepted**: Python-clock `created_at`/`expires_at` with
documented NTP-sync requirement, in exchange for keeping `_serde.py` and
the Lua script simple. The Redis-side `PEXPIRE` is the real eviction
authority — record fields are advisory metadata for the application
layer, not the source of truth for "is this expired".

### Operational notes

- **TTL metrics**: operators monitoring "time-to-eviction" should query
  Redis `PTTL` directly, not compute `record.expires_at - time.time()`.
  The record fields can drift on NTP step adjustments and would page on
  spurious negative values.
- **Cluster routing**: the acquire Lua script touches a single key
  (`{namespace}:{key}`), so it is safe under Redis Cluster routing —
  no cross-slot operations.

## Streaming response pass-through

**Status: v0.2.0.**

The middleware can't cache a streaming response — by definition the
body is produced incrementally, often larger than memory can buffer
(SSE feeds, file downloads, long-poll). Buffering would either OOM
the worker or delay the first byte until the stream finishes,
defeating the point of streaming. v0.2.0 chose to **forward live
and skip caching**: the slot is released, the response carries
`Idempotency-Stored: false`, and retries run the handler again.

### Detection: deferred-start with one-tick peek

ASGI delivers the response as a sequence of messages: one
`http.response.start`, then one or more `http.response.body` with
`more_body=True/False`. The streaming signal is `more_body=True` on
the first body chunk — but by the time it arrives, `start` has
typically already been forwarded, and we can no longer inject the
`Idempotency-Stored` header into start's headers.

Solution: `_ResponseInterceptor` defers `http.response.start` until the
first body chunk. On `more_body=True` we patch the deferred start
with the header and forward it live, followed by the body chunk;
subsequent messages take the streaming branch and pass through
verbatim. On `more_body=False` we treat the response as single-chunk,
buffer the body, and let the middleware emit a fresh start+body pair
after deciding cache vs. release (the v0.1.0 path).

The deferral is "until the first body chunk" — possibly many
event-loop ticks if the handler does work between `start` and the
first body emission. Apps that emit `start` for back-pressure
headers and only later produce data still work correctly: the
detection waits for the first body, not a fixed time.

### Why `Idempotency-Stored: false` is reused (not a new header)

The semantic of `Idempotency-Stored: false` is "this response will
not replay on retry — the slot is gone." That holds for both paths
that emit it: storage failure (v0.1.0) and streaming pass-through
(v0.2.0). Inventing a separate header (e.g., `Idempotency-Streamed`)
would force clients to handle two signals carrying the same
operational meaning. The header is documented as a union in README.

### Invariant: cached responses are never streams

`store.complete` is unreachable for streamed responses (`_run_and_cache`
returns immediately on `interceptor.streamed`). So every `CachedResponse`
in the store represents a single buffered frame — REPLAY can emit
them via `_send_response` without per-record streaming logic. Future
contributors adding a `complete_streaming` path would have to
preserve this invariant or rework the REPLAY emit.

## Volatile response headers stripped before caching

**Status: v0.2.0.**

Caching response headers verbatim is a security bug: `Set-Cookie`
carries session tokens, `Authorization` echoes leak credentials, and
`WWW-Authenticate` challenges encode realm/nonce state tied to the
first caller. Re-emitting any of these on replay hands them to
whoever presents the same `Idempotency-Key` — a session-theft
primitive in shared-store or multi-tenant deployments.

`_run_and_cache` therefore filters a hard-coded denylist before
constructing `CachedResponse`. The **first** caller still receives
the handler's original headers untouched; only the stored copy (and
the REPLAY emission derived from it) is filtered.

Denylist (`_VOLATILE_HEADER_DENYLIST` in `middleware.py`):

- Identity-bearing: `set-cookie`, `authorization`,
  `proxy-authorization`, `www-authenticate`, `proxy-authenticate`,
  `cookie`
- Connection-level (RFC 9110 §7.6.1 forbids caching): `connection`,
  `keep-alive`, `transfer-encoding`, `upgrade`, `trailer`

Hard-coded for v0.2.0. v0.3.0 may expose a constructor kwarg
(`volatile_headers=[...]`) so deployments with custom headers
(e.g. proprietary auth-context echoes) can extend the list.

## Logging — keys hashed, hot path silent

**Status: v0.2.0.**

Applications routinely use user-controlled identifiers (order IDs,
user emails, session tokens) as idempotency keys. Writing them to
centralized logs (Datadog, ELK, Splunk) creates PII retention beyond
the application's own controls. The middleware therefore hashes every
key before emitting any log record, and includes it as a structured
`key_hash` field alongside `fingerprint` and `outcome` for incident
correlation.

- **Hash function** — HMAC-SHA256 when `secret=` is configured for
  fingerprinting, plain SHA-256 otherwise. Reusing the same secret
  avoids a second config knob.
  - Under HMAC, a log reader without the secret cannot recompute the
    hash of a guessed key — this is the security property.
  - Under plain SHA-256 (`secret=None`), the log hash is **not**
    unlinkable: a log reader with a list of candidate keys (e.g. user
    IDs, order numbers) can recompute their hashes and re-identify
    log entries. `secret=None` is the v0.1.0 fallback for tests and
    migration; production deployments should always set a secret.
  - Rotate `IDEMP_SECRET` on incident — secret compromise enables
    both fingerprint forgery and log-hash recomputation.
- **Truncation** — 12 hex chars (48 bits). Collision-resistant for
  correlation within a typical retention window (birthday at ~2^24
  keys), not a primary key and not reversible to recover the source.
- **What gets logged** — `MISMATCH` (WARNING; potential probe or
  client bug), `IN_FLIGHT` (INFO; normal concurrency), `StoreError`,
  handler exceptions, and the no-response 500 path. `CREATED` /
  `REPLAY` are silent — they're the hot path and would drown other
  signals. Validation paths (400, 413, passthrough) are the access
  log's responsibility.
- **Structured fields** land on `LogRecord` via `extra=`, so
  structlog / python-json-logger consumers get them as JSON keys:
  - `key_hash` — always present.
  - `fingerprint` — 12-hex truncation of the body fingerprint
    (present on `MISMATCH` and `IN_FLIGHT`).
  - `outcome` — the `AcquireOutcome` value (`mismatch`, `in_flight`).
  - `status` — HTTP status code as string (`"409"`, `"422"`, `"500"`).
  - `exc_type` — exception class name (handler-exception path only).
- **No `exc_info=True`** on the handler-exception path — a downstream
  handler may raise with the raw key in its message, and the
  formatter would render the traceback unredacted. The exception
  type lands in `exc_type` for triage; the surrounding ASGI server
  is the right home for full traces.

## Why msgpack over JSON

**Status: v0.2.0.**

`IdempotencyRecord.response.body` is `bytes` and `headers` is
`tuple[tuple[bytes, bytes], ...]`. JSON has no native bytes type — a
JSON-based codec must base64-encode every body and every header value
(~33% size overhead plus encode/decode CPU on every read/write). On a
1 MiB body that's 350 KiB of avoidable storage and ~3 ms of CPU per
round-trip; on a Redis-backed deployment with a 24 h `completed_ttl`,
that compounds across millions of cached records.

msgpack handles bytes natively (the `bin` type), preserves tuples vs
lists (relevant for the headers shape), and round-trips deterministically
when fed the same input. The codec is wrapped in a v1 envelope
(`{"v": 1, "data": ...}`) so a future schema change can ship a v2
decoder without breaking already-stored v1 records.

Pickle was ruled out at design time: it's a remote-code-execution
sink on untrusted bytes, which is exactly what a compromised Redis
becomes. msgpack with `strict_map_key=True` + bounded `max_bin_len`
/ `max_str_len` / `max_array_len` / `max_map_len` is the inverse —
attacker-controlled bytes can't allocate beyond the configured caps,
and an extra explicit `isinstance` validation pass on the decoded
dict catches type-confusion attempts (e.g. `key` arriving as `int`,
`body` as `str`, header pairs as 3-tuples that would unpack
silently).

## Key scoping (`scope_factory`)

**Status: planned for v0.3.0.** Not yet implemented.

The current key-extraction reads `Idempotency-Key` from the request
headers. Multi-tenant deployments often want to scope the key by
something else — tenant ID, authenticated user, route prefix — so
two tenants sharing the wire-level header don't collide. `scope_factory`
will be a callable `(scope) -> bytes` injected into the middleware that
prefixes the raw header value before the store sees it.
