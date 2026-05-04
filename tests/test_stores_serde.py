"""Round-trip and validation tests for the msgpack record codec."""

from __future__ import annotations

import msgpack
import pytest

from fastapi_idempotency import (
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
    StoreError,
)
from fastapi_idempotency.stores._serde import (
    SCHEMA_VERSION,
    decode_record,
    encode_record,
)


def _make_record(*, response: CachedResponse | None = None) -> IdempotencyRecord:
    state = IdempotencyState.COMPLETED if response is not None else IdempotencyState.IN_FLIGHT
    return IdempotencyRecord(
        key=IdempotencyKey("order-123"),
        fingerprint=Fingerprint("0123abcdef"),
        state=state,
        created_at=1234567890.123,
        expires_at=1234567920.456,
        response=response,
    )


def _envelope_with_data(data: dict[str, object]) -> bytes:
    """Build a v=1 envelope with the given record-data dict."""
    return msgpack.packb({"v": SCHEMA_VERSION, "data": data}, use_bin_type=True)  # type: ignore[no-any-return]


def _valid_record_data(**overrides: object) -> dict[str, object]:
    """Baseline valid record dict; tests override single fields to provoke errors."""
    base: dict[str, object] = {
        "key": "k",
        "fingerprint": "fp",
        "state": "in_flight",
        "created_at": 1.0,
        "expires_at": 2.0,
        "response": None,
    }
    base.update(overrides)
    return base


def _valid_response_data(**overrides: object) -> dict[str, object]:
    """Baseline valid response dict for inner CachedResponse fields."""
    base: dict[str, object] = {
        "status_code": 200,
        "headers": [],
        "body": b"",
        "media_type": None,
    }
    base.update(overrides)
    return base


# === Round-trip ===


def test_round_trip_in_flight_record() -> None:
    original = _make_record()

    decoded = decode_record(encode_record(original))

    assert decoded == original


def test_round_trip_completed_with_response() -> None:
    response = CachedResponse(
        status_code=201,
        headers=(
            (b"content-type", b"application/json"),
            (b"x-custom", b"value"),
        ),
        body=b'{"id": 42}',
        media_type="application/json",
    )
    original = _make_record(response=response)

    decoded = decode_record(encode_record(original))

    assert decoded == original
    assert decoded.response is not None
    assert isinstance(decoded.response.headers, tuple)
    for pair in decoded.response.headers:
        assert isinstance(pair, tuple)
        assert isinstance(pair[0], bytes)
        assert isinstance(pair[1], bytes)


def test_round_trip_empty_body_and_headers() -> None:
    response = CachedResponse(status_code=204, headers=(), body=b"")
    original = _make_record(response=response)

    decoded = decode_record(encode_record(original))

    assert decoded == original


def test_round_trip_preserves_none_response() -> None:
    record = _make_record()

    decoded = decode_record(encode_record(record))

    assert decoded.response is None


# === Schema envelope ===


def test_envelope_carries_schema_version() -> None:
    record = _make_record()

    envelope = msgpack.unpackb(encode_record(record), raw=False)

    assert envelope["v"] == SCHEMA_VERSION
    assert "data" in envelope


def test_decode_rejects_unknown_schema_version() -> None:
    record = _make_record()
    envelope = msgpack.unpackb(encode_record(record), raw=False)
    envelope["v"] = SCHEMA_VERSION + 99
    bad_bytes = msgpack.packb(envelope, use_bin_type=True)

    with pytest.raises(StoreError, match="unsupported record schema version"):
        decode_record(bad_bytes)


def test_decode_rejects_non_dict_envelope() -> None:
    bad = msgpack.packb([1, 2, 3], use_bin_type=True)

    with pytest.raises(StoreError, match="not a dict"):
        decode_record(bad)


def test_decode_rejects_envelope_missing_data() -> None:
    bad = msgpack.packb({"v": SCHEMA_VERSION}, use_bin_type=True)

    with pytest.raises(StoreError, match="missing 'data'"):
        decode_record(bad)


# === Malformed input ===


def test_decode_rejects_malformed_bytes() -> None:
    with pytest.raises(StoreError):
        decode_record(b"\x00\x01\x02 not msgpack")


def test_decode_rejects_empty_bytes() -> None:
    with pytest.raises(StoreError):
        decode_record(b"")


# === KeyError / ValueError → StoreError contract ===


def test_decode_missing_field_raises_store_error() -> None:
    bad = msgpack.packb(
        {"v": SCHEMA_VERSION, "data": {"key": "abc"}},  # missing fingerprint, etc
        use_bin_type=True,
    )

    with pytest.raises(StoreError, match="invalid record fields"):
        decode_record(bad)


def test_decode_unknown_state_raises_store_error() -> None:
    bad = _envelope_with_data(_valid_record_data(state="deleted"))

    with pytest.raises(StoreError):
        decode_record(bad)


# === Field type validation ===


def test_decode_rejects_int_as_key() -> None:
    bad = _envelope_with_data(_valid_record_data(key=42))

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_int_as_fingerprint() -> None:
    bad = _envelope_with_data(_valid_record_data(fingerprint=42))

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_inf_timestamp() -> None:
    bad = _envelope_with_data(_valid_record_data(created_at=float("inf")))

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_nan_timestamp() -> None:
    bad = _envelope_with_data(_valid_record_data(expires_at=float("nan")))

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_string_as_timestamp() -> None:
    bad = _envelope_with_data(_valid_record_data(created_at="never"))

    with pytest.raises(StoreError):
        decode_record(bad)


# === Response field validation ===


def test_decode_rejects_invalid_status_code() -> None:
    bad = _envelope_with_data(
        _valid_record_data(
            state="completed",
            response=_valid_response_data(status_code=999),
        ),
    )

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_str_as_body() -> None:
    bad = _envelope_with_data(
        _valid_record_data(
            state="completed",
            response=_valid_response_data(body="not bytes"),
        ),
    )

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_int_as_status_code() -> None:
    """status_code 200 (int) is valid; True (bool subclass) must not pass."""
    bad = _envelope_with_data(
        _valid_record_data(
            state="completed",
            response=_valid_response_data(status_code=True),
        ),
    )

    with pytest.raises(StoreError):
        decode_record(bad)


# === Header validation (_coerce_header_pairs) ===


def test_decode_rejects_3_element_header_pair() -> None:
    bad = _envelope_with_data(
        _valid_record_data(
            state="completed",
            response=_valid_response_data(headers=[[b"a", b"b", b"c"]]),
        ),
    )

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_int_as_header_value() -> None:
    """Guard against ``bytes(int)`` memory-blow-up: ``bytes(1073741824)`` → 1 GiB."""
    bad = _envelope_with_data(
        _valid_record_data(
            state="completed",
            response=_valid_response_data(headers=[[b"name", 1073741824]]),
        ),
    )

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_str_as_header_name() -> None:
    bad = _envelope_with_data(
        _valid_record_data(
            state="completed",
            response=_valid_response_data(headers=[["content-type", b"value"]]),
        ),
    )

    with pytest.raises(StoreError):
        decode_record(bad)


def test_decode_rejects_oversized_headers_list() -> None:
    too_many = [[b"x", b"y"] for _ in range(101)]
    bad = _envelope_with_data(
        _valid_record_data(
            state="completed",
            response=_valid_response_data(headers=too_many),
        ),
    )

    with pytest.raises(StoreError):
        decode_record(bad)


# === Forward-compat ===


def test_decode_ignores_unknown_data_fields() -> None:
    """v=2 may add new fields; v=1 readers should not crash on them."""
    record = _make_record()
    encoded = encode_record(record)
    envelope = msgpack.unpackb(encoded, raw=False)
    envelope["data"]["future_field"] = "extra"
    bytes_with_extra = msgpack.packb(envelope, use_bin_type=True)

    decoded = decode_record(bytes_with_extra)

    assert decoded == record


# === Wire-format pinning (silent rename detector) ===


def test_wire_format_state_strings_are_stable() -> None:
    """State enum values are wire format. Renames must trigger a v=2 bump."""
    in_flight = encode_record(_make_record())
    assert b"in_flight" in in_flight

    completed_response = CachedResponse(status_code=200, headers=(), body=b"")
    completed = encode_record(_make_record(response=completed_response))
    assert b"completed" in completed


# === Determinism ===


def test_encode_is_deterministic() -> None:
    """Same input → same bytes. Required for cross-language msgpack peers."""
    record = _make_record()

    assert encode_record(record) == encode_record(record)


def test_encode_high_precision_timestamp_is_deterministic() -> None:
    """High-precision timestamps must round-trip bit-exactly across encodes.

    Cross-language msgpack peers (Rust ``rmp-serde``, Go ``msgpack/v5``) may
    round-trip floats through float32 paths in mixed-language deployments;
    pinning a determinism test now locks the contract for v=1 so any future
    drift surfaces as a failing test instead of silent data-store corruption.
    """
    record = IdempotencyRecord(
        key=IdempotencyKey("k"),
        fingerprint=Fingerprint("fp"),
        state=IdempotencyState.IN_FLIGHT,
        created_at=1234567890.123456789,
        expires_at=1234567920.987654321,
        response=None,
    )

    a = encode_record(record)
    b = encode_record(record)

    assert a == b

    decoded = decode_record(a)
    assert decoded.created_at == record.created_at
    assert decoded.expires_at == record.expires_at


# === Resource limits ===


def test_decode_rejects_oversized_body() -> None:
    """Body > 1 MiB (max_bin_len) must be rejected by msgpack unpack."""
    huge_body = b"x" * (1024 * 1024 + 100)
    record = _make_record(
        response=CachedResponse(status_code=200, headers=(), body=huge_body),
    )
    encoded = encode_record(record)  # encoder has no limit

    with pytest.raises(StoreError):
        decode_record(encoded)
