"""Request fingerprinting used to detect Idempotency-Key reuse with altered payloads."""

from __future__ import annotations

import hashlib

from .types import Fingerprint


def compute_fingerprint(
    method: str,
    path: str,
    query_string: bytes,
    body: bytes,
) -> Fingerprint:
    """Compute a stable fingerprint over ``method + path + query + body``.

    Returns a hex-encoded SHA-256 digest. Each component is length-prefixed
    before being fed to the hasher so prefix-aligned ambiguity is
    impossible (e.g., ``method="POS", path="T/foo"`` cannot collide with
    ``method="POST", path="/foo"``).

    Argument types follow the ASGI scope shape: ``path`` is ``str``,
    ``query_string`` is raw ``bytes``.
    """
    h = hashlib.sha256()
    for part in (
        method.encode("ascii"),
        path.encode("utf-8"),
        query_string,
        body,
    ):
        h.update(len(part).to_bytes(4, "big"))
        h.update(part)
    return Fingerprint(h.hexdigest())
