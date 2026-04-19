"""Request fingerprinting used to detect Idempotency-Key reuse with altered payloads."""

from __future__ import annotations

from .types import Fingerprint


def compute_fingerprint(
    method: str,
    path: str,
    query_string: bytes,
    body: bytes,
) -> Fingerprint:
    """Compute a stable fingerprint over ``method + path + query + body``.

    Returns a hex-encoded SHA-256 digest. Two requests that share an
    ``Idempotency-Key`` but disagree on any of these components are a
    mismatch and the middleware will respond with ``422``. Query string
    is included because it typically carries request semantics
    (``?tenant=``, ``?dry_run=``, pagination, etc.).

    Argument types follow the ASGI scope shape: ``path`` is ``str``,
    ``query_string`` is raw ``bytes``.
    """
    raise NotImplementedError
