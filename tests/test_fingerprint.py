"""Tests for compute_fingerprint."""

from __future__ import annotations

import pytest

from fastapi_idempotency.fingerprint import compute_fingerprint


def test_returns_64_char_hex_sha256() -> None:
    fp = compute_fingerprint("POST", "/orders", b"", b"")

    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_deterministic_for_same_inputs() -> None:
    fp1 = compute_fingerprint("POST", "/orders", b"x=1", b'{"id":1}')
    fp2 = compute_fingerprint("POST", "/orders", b"x=1", b'{"id":1}')

    assert fp1 == fp2


def test_method_change_changes_fingerprint() -> None:
    a = compute_fingerprint("POST", "/orders", b"", b"")
    b = compute_fingerprint("PUT", "/orders", b"", b"")

    assert a != b


def test_path_change_changes_fingerprint() -> None:
    a = compute_fingerprint("POST", "/orders", b"", b"")
    b = compute_fingerprint("POST", "/orders/1", b"", b"")

    assert a != b


def test_query_string_change_changes_fingerprint() -> None:
    a = compute_fingerprint("POST", "/orders", b"x=1", b"")
    b = compute_fingerprint("POST", "/orders", b"x=2", b"")

    assert a != b


def test_body_change_changes_fingerprint() -> None:
    a = compute_fingerprint("POST", "/orders", b"", b'{"id":1}')
    b = compute_fingerprint("POST", "/orders", b"", b'{"id":2}')

    assert a != b


def test_prefix_aligned_inputs_do_not_collide() -> None:
    """method='POS' + path='T/foo' must not equal method='POST' + path='/foo'."""
    a = compute_fingerprint("POST", "/foo", b"", b"")
    b = compute_fingerprint("POS", "T/foo", b"", b"")

    assert a != b


def test_empty_body_works() -> None:
    fp = compute_fingerprint("POST", "/orders", b"", b"")

    assert len(fp) == 64


# ---------------------------------------------------------------------------
# HMAC mode (secret=bytes)
# ---------------------------------------------------------------------------


def test_hmac_returns_64_char_hex_sha256() -> None:
    fp = compute_fingerprint("POST", "/orders", b"", b"", secret=b"k")

    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_hmac_deterministic_for_same_secret_and_inputs() -> None:
    fp1 = compute_fingerprint("POST", "/orders", b"x=1", b'{"id":1}', secret=b"k")
    fp2 = compute_fingerprint("POST", "/orders", b"x=1", b'{"id":1}', secret=b"k")

    assert fp1 == fp2


def test_hmac_different_secret_changes_fingerprint() -> None:
    """The point of HMAC: same inputs, different secret → different fingerprint
    (so an attacker without the secret can't reproduce fingerprints)."""
    a = compute_fingerprint("POST", "/orders", b"", b"", secret=b"secret-1")
    b = compute_fingerprint("POST", "/orders", b"", b"", secret=b"secret-2")

    assert a != b


def test_hmac_differs_from_plain_sha256() -> None:
    """HMAC vs plain: same inputs but secret=None vs secret=b'...' must
    produce different fingerprints — stores cannot be shared across modes."""
    plain = compute_fingerprint("POST", "/orders", b"", b"", secret=None)
    hmac_fp = compute_fingerprint("POST", "/orders", b"", b"", secret=b"k")

    assert plain != hmac_fp


def test_hmac_body_change_still_changes_fingerprint() -> None:
    """Length-prefix scheme + HMAC: a body flip flips the fingerprint."""
    a = compute_fingerprint("POST", "/orders", b"", b'{"id":1}', secret=b"k")
    b = compute_fingerprint("POST", "/orders", b"", b'{"id":2}', secret=b"k")

    assert a != b


def test_hmac_prefix_aligned_inputs_do_not_collide() -> None:
    """Length-prefix defense holds under HMAC too."""
    a = compute_fingerprint("POST", "/foo", b"", b"", secret=b"k")
    b = compute_fingerprint("POS", "T/foo", b"", b"", secret=b"k")

    assert a != b


def test_hmac_empty_secret_rejected() -> None:
    """``secret=b""`` is a cryptographically trivial HMAC key. The
    explicit "no HMAC" opt-out is ``secret=None``."""
    with pytest.raises(ValueError, match="non-empty"):
        compute_fingerprint("POST", "/orders", b"", b"", secret=b"")


def test_hmac_long_secret_above_block_size() -> None:
    """HMAC pre-hashes keys longer than the block size (64 bytes for
    SHA-256). Verify the long-key path still produces a stable digest
    and stays deterministic."""
    long_secret = b"k" * 200  # 3x block size
    a = compute_fingerprint("POST", "/orders", b"", b"body", secret=long_secret)
    b = compute_fingerprint("POST", "/orders", b"", b"body", secret=long_secret)
    different = compute_fingerprint(
        "POST",
        "/orders",
        b"",
        b"body",
        secret=long_secret + b"x",
    )

    assert a == b
    assert a != different


def test_hmac_non_ascii_secret_bytes() -> None:
    """Secret bytes are opaque to the codec — non-ASCII payloads work."""
    fp = compute_fingerprint(
        "POST",
        "/orders",
        b"",
        b"body",
        secret=b"\xf0\x9f\x94\x91",
    )

    assert len(fp) == 64
