"""Serialization/deserialization helpers for store-persisted records.

Records are serialized as msgpack-encoded envelopes with an explicit schema
version, so the on-disk shape can evolve in future minor releases without
breaking already-stored records. Read paths verify the envelope version,
validate field types/ranges defensively, and raise :class:`StoreError` on
any malformed input.

Wire format::

    {
        "v": 1,                          # schema version
        "data": { ... record fields ... }
    }

This module is private (underscore-prefixed) — its API is an implementation
detail of the store backends, not part of the public package surface.

Logging note: error paths populate ``logger.debug(..., extra={...})`` with
``version`` and ``expected`` fields so structured loggers (Datadog, Loki,
ELK with JSON formatters) can grep deploy-incident logs in seconds. The
fallback message string also embeds the values for plain-formatter logs.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import msgpack

from fastapi_idempotency.errors import StoreError
from fastapi_idempotency.types import (
    CachedResponse,
    Fingerprint,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# TODO(v=2): introduce MIN_SUPPORTED_VERSION when adding a new schema version,
# and switch to a decoder registry (`_DECODERS = {1: _v1, 2: _v2}`) so old
# records remain readable during rolling deploys.

# Unpack limits — sized to the v0.3.0 production max_body_bytes default
# (1 MiB) plus envelope overhead and headroom. These caps prevent
# memory-DoS via attacker-crafted oversized payloads.
_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
_MAX_BUFFER_SIZE = 2 * _MAX_BODY_BYTES  # body + envelope + headroom
_MAX_BIN_LEN = _MAX_BODY_BYTES
_MAX_STR_LEN = 8 * 1024  # 8 KiB — header values realistic limit
_MAX_ARRAY_LEN = 256  # max headers + safety margin
_MAX_MAP_LEN = 16  # envelope + record fields
_MAX_HEADERS = 100

# HTTP status code valid range (inclusive lower, exclusive upper).
_VALID_STATUS_RANGE = (100, 600)

# Each header is a (name, value) pair — exactly 2 elements.
_HEADER_PAIR_LEN = 2

# Truncation length for exception repr in error messages — defends
# against log-volume DoS via attacker-crafted multi-MB blob whose repr
# would otherwise blow up log lines.
_EXC_REPR_LIMIT = 200


def encode_record(record: IdempotencyRecord) -> bytes:
    """Serialize an :class:`IdempotencyRecord` to versioned msgpack bytes.

    The resulting bytes are deterministic across calls (same input → same
    bytes); see ``test_encode_is_deterministic`` for the pinned contract.
    """
    envelope = {
        "v": SCHEMA_VERSION,
        "data": _record_to_dict(record),
    }
    return msgpack.packb(envelope, use_bin_type=True)  # type: ignore[no-any-return]


def decode_record(data: bytes) -> IdempotencyRecord:
    """Deserialize msgpack bytes (as written by :func:`encode_record`).

    Raises :class:`StoreError` on:
      * malformed msgpack bytes,
      * input bytes exceeding the configured buffer size,
      * envelope shape violations,
      * unknown schema version,
      * any field validation failure (wrong type, out-of-range, missing).
    """
    if len(data) > _MAX_BUFFER_SIZE:
        msg = f"record bytes ({len(data)}) exceed buffer size limit ({_MAX_BUFFER_SIZE})"
        raise StoreError(msg)

    try:
        envelope = msgpack.unpackb(
            data,
            raw=False,
            strict_map_key=True,
            max_bin_len=_MAX_BIN_LEN,
            max_str_len=_MAX_STR_LEN,
            max_array_len=_MAX_ARRAY_LEN,
            max_map_len=_MAX_MAP_LEN,
        )
    except (msgpack.exceptions.UnpackException, ValueError, TypeError) as exc:
        msg = f"malformed record bytes: {repr(exc)[:_EXC_REPR_LIMIT]}"
        raise StoreError(msg) from exc

    if not isinstance(envelope, dict):
        msg = f"record envelope is not a dict: {type(envelope).__name__}"
        raise StoreError(msg)

    version = envelope.get("v")
    if version != SCHEMA_VERSION:
        msg = (
            f"unsupported record schema version {version!r}; "
            f"this build understands {SCHEMA_VERSION}"
        )
        logger.debug(
            "_serde: rejecting unsupported version",
            extra={"version": version, "expected": SCHEMA_VERSION},
        )
        raise StoreError(msg)

    data_dict = envelope.get("data")
    if not isinstance(data_dict, dict):
        msg = "record envelope missing 'data' dict"
        raise StoreError(msg)

    try:
        return _dict_to_record(data_dict)
    except (KeyError, ValueError, TypeError) as exc:
        msg = f"invalid record fields: {repr(exc)[:_EXC_REPR_LIMIT]}"
        raise StoreError(msg) from exc


def _record_to_dict(record: IdempotencyRecord) -> dict[str, Any]:
    return {
        "key": str(record.key),
        "fingerprint": str(record.fingerprint),
        "state": record.state.value,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "response": (_response_to_dict(record.response) if record.response is not None else None),
    }


def _dict_to_record(d: dict[str, Any]) -> IdempotencyRecord:
    """Construct ``IdempotencyRecord`` from decoded dict.

    Raises ``KeyError`` on missing fields, ``ValueError`` on invalid values,
    ``TypeError`` on wrong types — all wrapped into ``StoreError`` by the
    caller (:func:`decode_record`).
    """
    # NewType is a no-op at runtime — we validate each field explicitly to
    # catch type-confusion attacks (e.g., key as int, body as str).

    key_value = d["key"]
    if not isinstance(key_value, str):
        msg = f"key must be str, got {type(key_value).__name__}"
        raise TypeError(msg)

    fingerprint_value = d["fingerprint"]
    if not isinstance(fingerprint_value, str):
        msg = f"fingerprint must be str, got {type(fingerprint_value).__name__}"
        raise TypeError(msg)

    state_value = d["state"]  # KeyError if missing → wrapped by caller
    state = IdempotencyState(state_value)  # ValueError if unknown → wrapped

    created_at = d["created_at"]
    if not isinstance(created_at, (int, float)) or not math.isfinite(created_at):
        msg = f"created_at must be finite number, got {created_at!r}"
        raise TypeError(msg)

    expires_at = d["expires_at"]
    if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
        msg = f"expires_at must be finite number, got {expires_at!r}"
        raise TypeError(msg)

    response_dict = d.get("response")
    response = _dict_to_response(response_dict) if response_dict is not None else None

    return IdempotencyRecord(
        key=IdempotencyKey(key_value),
        fingerprint=Fingerprint(fingerprint_value),
        state=state,
        created_at=float(created_at),
        expires_at=float(expires_at),
        response=response,
    )


def _response_to_dict(response: CachedResponse) -> dict[str, Any]:
    return {
        "status_code": response.status_code,
        "headers": [list(pair) for pair in response.headers],
        "body": response.body,
        "media_type": response.media_type,
    }


def _dict_to_response(d: dict[str, Any]) -> CachedResponse:
    """Construct ``CachedResponse`` from decoded dict.

    See :func:`_coerce_header_pairs` for the structural validation of
    headers (arity + bytes-type — guards against ``bytes(int)``
    memory-blow-up attacks).
    """
    status_code = d["status_code"]
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        # bool is subclass of int — explicit reject
        msg = f"status_code must be int, got {type(status_code).__name__}"
        raise TypeError(msg)
    if not (_VALID_STATUS_RANGE[0] <= status_code < _VALID_STATUS_RANGE[1]):
        msg = f"status_code {status_code} outside HTTP status range"
        raise ValueError(msg)

    body = d["body"]
    if not isinstance(body, (bytes, bytearray)):
        msg = f"body must be bytes, got {type(body).__name__}"
        raise TypeError(msg)

    headers_raw = d["headers"]
    if not isinstance(headers_raw, list):
        msg = f"headers must be list, got {type(headers_raw).__name__}"
        raise TypeError(msg)
    if len(headers_raw) > _MAX_HEADERS:
        msg = f"headers count {len(headers_raw)} exceeds {_MAX_HEADERS}"
        raise ValueError(msg)
    headers = _coerce_header_pairs(headers_raw)

    media_type = d["media_type"]
    if media_type is not None and not isinstance(media_type, str):
        msg = f"media_type must be str or None, got {type(media_type).__name__}"
        raise TypeError(msg)

    return CachedResponse(
        status_code=status_code,
        headers=headers,
        body=bytes(body),  # bytearray → bytes for immutability
        media_type=media_type,
    )


def _coerce_header_pairs(
    raw: list[Any],
) -> tuple[tuple[bytes, bytes], ...]:
    """Validate and coerce a list of header pairs.

    Each pair must be a 2-element list/tuple of bytes. Guards against:
      * arity confusion (3-tuple sneaking through into iteration unpack);
      * ``bytes(int)`` memory-blow-up — if a decoded value is an int,
        ``bytes(N)`` allocates N null bytes, so a stored value of
        ``2**30`` blows up to 1 GiB before any sanity check.
    """
    result: list[tuple[bytes, bytes]] = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)):
            msg = f"header pair must be list/tuple, got {type(pair).__name__}"
            raise TypeError(msg)
        if len(pair) != _HEADER_PAIR_LEN:
            msg = f"header pair must have {_HEADER_PAIR_LEN} elements, got {len(pair)}"
            raise ValueError(msg)
        name, value = pair
        if not isinstance(name, (bytes, bytearray)):
            msg = f"header name must be bytes, got {type(name).__name__}"
            raise TypeError(msg)
        if not isinstance(value, (bytes, bytearray)):
            msg = f"header value must be bytes, got {type(value).__name__}"
            raise TypeError(msg)
        result.append((bytes(name), bytes(value)))
    return tuple(result)
