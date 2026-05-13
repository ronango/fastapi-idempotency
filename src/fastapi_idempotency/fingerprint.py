"""Request fingerprinting used to detect Idempotency-Key reuse with altered payloads."""

from __future__ import annotations

import hashlib
import hmac

from .types import Fingerprint


def compute_fingerprint(
    method: str,
    path: str,
    query_string: bytes,
    body: bytes,
    *,
    secret: bytes | None = None,
) -> Fingerprint:
    """Compute a stable fingerprint over ``method + path + query + body``.

    Returns a hex-encoded digest. Each component is length-prefixed
    before being fed to the hasher so prefix-aligned ambiguity is
    impossible (e.g., ``method="POS", path="T/foo"`` cannot collide
    with ``method="POST", path="/foo"``).

    When ``secret`` is provided, switches to HMAC-SHA256 — without the
    secret an attacker can't construct fingerprints for arbitrary
    bodies, so they can't probe via ``MISMATCH``/``REPLAY`` outcomes.
    Plain SHA-256 (``secret=None``) is kept for v0.1.0 backward
    compatibility but is explicitly insecure for shared-store deployments.

    Empty bytes (``secret=b""``) is rejected — it's a cryptographically
    trivial HMAC key. The explicit "no HMAC" opt-out is ``secret=None``.
    """
    if secret is not None and len(secret) == 0:
        msg = "HMAC secret must be non-empty; use secret=None to opt out explicitly"
        raise ValueError(msg)
    h = hmac.new(secret, digestmod=hashlib.sha256) if secret is not None else hashlib.sha256()
    for part in (
        method.encode("ascii"),
        path.encode("utf-8"),
        query_string,
        body,
    ):
        h.update(len(part).to_bytes(4, "big"))
        h.update(part)
    return Fingerprint(h.hexdigest())
