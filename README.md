# fastapi-idempotency

> Opinionated `Idempotency-Key` middleware for FastAPI / Starlette.
> Implements the IETF draft spec with explicit handling of concurrent retries, body-fingerprint mismatch, and failed requests.
> Pluggable backends (in-memory, Redis), `mypy --strict`, targets 95%+ test coverage.

## Status

**Pre-release / experimental.** The public API is not stable; expect breaking changes before v0.2.0. Do not use in production yet.

## Quickstart

Install (the package is on TestPyPI while in pre-release):

```bash
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  fastapi-idempotency
```

`--extra-index-url` lets pip resolve runtime dependencies (`starlette`,
`msgpack`) from real PyPI, since TestPyPI mirrors only this package.

### FastAPI

```python
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_idempotency import IdempotencyMiddleware, InMemoryStore


class Order(BaseModel):
    item_id: int
    quantity: int


app = FastAPI()
app.add_middleware(IdempotencyMiddleware, store=InMemoryStore())


@app.post("/orders")
async def create_order(order: Order) -> dict[str, int]:
    # Your business logic — runs at most once per Idempotency-Key.
    return {"id": 42, "item_id": order.item_id}
```

Send the same request twice with `Idempotency-Key: abc-123`:

- The first call runs the handler and returns `{"id": 42, ...}`.
- The second call returns the same body with `Idempotent-Replayed: true` —
  no second insert, no second charge.

### Starlette

```python
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from fastapi_idempotency import IdempotencyMiddleware, InMemoryStore


async def create_order(request):
    body = await request.json()
    return JSONResponse({"id": 42, "item_id": body["item_id"]})


app = Starlette(routes=[Route("/orders", create_order, methods=["POST"])])
app = IdempotencyMiddleware(app, InMemoryStore())
```

### Redis (multi-worker / multi-process)

`InMemoryStore` is single-process. For multi-worker deployments
(uvicorn `--workers > 1`, gunicorn, k8s replicas) use `RedisStore` so
all workers see the same idempotency state.

Install with the `[redis]` extra:

```bash
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  'fastapi-idempotency[redis]'
```

```python
import redis.asyncio

from fastapi import FastAPI
from fastapi_idempotency import IdempotencyMiddleware, RedisStore

# Production-recommended client config — see RedisStore docstring for
# the full rationale (connection-pool sizing, socket timeouts, etc).
client = redis.asyncio.Redis(
    connection_pool=redis.asyncio.BlockingConnectionPool(
        max_connections=200,    # tune per RPS x p99 latency
        timeout=2.0,            # bound queue wait on saturation
    ),
    socket_timeout=1.0,         # bound stalled-Redis cascades
    socket_connect_timeout=1.0,
)

app = FastAPI()
app.add_middleware(IdempotencyMiddleware, store=RedisStore(client))
```

Atomicity: `acquire` is a single Lua `EVAL`, so concurrent workers
cannot double-claim a key. The client must be constructed with
`decode_responses=False` (the default) — the codec operates on raw
bytes and `decode_responses=True` would silently corrupt msgpack.
TTL eviction uses Redis `PEXPIRE`; record fields are advisory metadata
(query Redis `PTTL` for the eviction authority).

## How it works

The middleware watches non-safe HTTP methods (POST/PATCH/PUT/DELETE) for
the `Idempotency-Key` header. Outcomes:

| Situation | What the client sees |
| --- | --- |
| First request with a given key | Handler runs; response is returned and cached. |
| Same key + same body, while the first is still in flight | `409 Conflict` |
| Same key + same body, after the first completed | Cached response with `Idempotent-Replayed: true` header |
| Same key + different body | `422 Unprocessable Entity` |
| Handler returned 5xx | Slot released; a retry will run the handler again. |
| Handler succeeded but the store failed to persist | Response delivered with `Idempotency-Stored: false` header |

Safe methods (GET/HEAD/OPTIONS), requests without an `Idempotency-Key`,
and requests over `max_body_bytes` pass through untouched.

## Roadmap

- **v0.1.0** — minimal working version: in-memory store, middleware core, fingerprint, two-phase TTL, replay path, basic error handling. Publish to TestPyPI.
- **v0.2.0** — production-ready: Redis backend, `409 Conflict` on concurrent requests, `422` on body mismatch, streaming pass-through, full CI matrix (3.10–3.13), PyPI release via Trusted Publisher.
- **v0.3.0** — ergonomics: lifecycle hooks (`on_replay`, `on_conflict`, `on_mismatch`), per-route config, per-user scoping, benchmarks.
- **v0.4.0** — polish: full docs site, cookbook (payments, webhooks), release-notes automation.
