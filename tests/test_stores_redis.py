"""Import-only smoke test for the Redis store stub.

Touching the module guarantees its top-level statements are measured by
coverage (otherwise the stub would show 0%, dragging total coverage down).
The class itself is marked ``# pragma: no cover`` until v0.2.0 lands the
real implementation.
"""

from __future__ import annotations

from fastapi_idempotency.stores import redis as redis_module


def test_redis_store_module_imports_without_redis_extra() -> None:
    """Module must import even when the ``redis`` extra isn't installed.

    Real ``redis.asyncio.Redis`` is referenced only under ``TYPE_CHECKING``;
    the runtime import path doesn't touch it, so the module is safe to
    import in environments without the optional dependency.
    """
    assert redis_module.RedisStore is not None
