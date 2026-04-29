"""Tests for compute_fingerprint."""

from __future__ import annotations

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
