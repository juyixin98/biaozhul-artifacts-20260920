"""Versioned share serialisation (format SSS/1).

A share carries the scheme parameters it was created with so the recovery
service never has to guess them. Two equivalent wire forms are supported:

1. Compact binary, base64url (no padding), prefixed ``SSS1$``::

       "SSS1" | flags(1, must be 0) | x(1) | threshold(2 BE) | total(2 BE)
              | y_0(32 BE) | y_1(32 BE) | ...

   Each y is a GF(p) element encoded big-endian in 32 bytes. The block count
   is derived from the body length; the secret length itself is deliberately
   NOT stored per share (it lives inside the shared data, so it cannot be
   spoofed from a single share without breaking reconstruction).

2. JSON object (also returned by the HTTP API)::

       {"version": 1, "x": 3, "threshold": 3, "total": 5,
        "y": ["base64url(32-byte field element)", ...]}

Malformed inputs raise ShareEncodingError; a parseable but future/unknown
format version raises UnsupportedVersionError. Decoding is strict: out of
range values, wrong-length fields, non-canonical base64 and y >= p all fail.
"""

import base64
import binascii
import json

from . import field
from .scheme import Share, MAX_TOTAL_SHARES, PARAM_VERSION

_MAGIC = b"SSS1"
_PREFIX = "SSS1$"
_FLAGS = 0
_Y_BYTES = 32
_HEADER_LEN = 4 + 1 + 1 + 2 + 2  # magic, flags, x, threshold, total
_BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class ShareEncodingError(ValueError):
    """The share cannot be decoded (corruption, truncation, bad field value)."""


class UnsupportedVersionError(ShareEncodingError):
    """The share parses but declares a format version this software lacks."""


def _pack(share: Share, threshold: int, total: int) -> bytes:
    if not 2 <= threshold <= MAX_TOTAL_SHARES:
        raise ShareEncodingError(f"threshold must be in 2..{MAX_TOTAL_SHARES}")
    if not threshold <= total <= MAX_TOTAL_SHARES:
        raise ShareEncodingError(f"total must satisfy threshold <= total <= {MAX_TOTAL_SHARES}")
    if not 1 <= share.x <= total:
        raise ShareEncodingError("share index x must be in 1..total")
    body = (
        _MAGIC
        + _FLAGS.to_bytes(1, "big")
        + share.x.to_bytes(1, "big")
        + threshold.to_bytes(2, "big")
        + total.to_bytes(2, "big")
    )
    for y in share.ys:
        if not 0 <= y < field.FIELD_PRIME:
            raise ShareEncodingError("y value is not an element of GF(p)")
        body += y.to_bytes(_Y_BYTES, "big")
    return body


def _unpack(raw: bytes):
    if len(raw) < _HEADER_LEN + _Y_BYTES:
        raise ShareEncodingError("share too short")
    if raw[:4] != _MAGIC:
        if raw[:4] == b"SSS2":
            raise UnsupportedVersionError("share format version 2 is not supported by this build")
        raise ShareEncodingError("bad magic bytes")
    flags = raw[4]
    x = raw[5]
    threshold = int.from_bytes(raw[6:8], "big")
    total = int.from_bytes(raw[8:10], "big")
    if x == 0:
        raise ShareEncodingError("share index x=0 is reserved for the secret")
    if not 2 <= threshold <= MAX_TOTAL_SHARES:
        raise ShareEncodingError("threshold out of range in share header")
    if not threshold <= total <= MAX_TOTAL_SHARES:
        raise ShareEncodingError("total out of range in share header")
    if not 1 <= x <= total:
        raise ShareEncodingError("share index outside 1..total")
    if flags != 0:
        raise UnsupportedVersionError(f"unknown share flags/extension bits: {flags}")
    tail = raw[_HEADER_LEN:]
    if len(tail) % _Y_BYTES != 0:
        raise ShareEncodingError("share body length is not a whole number of 32-byte blocks")
    ys = []
    for i in range(0, len(tail), _Y_BYTES):
        y = int.from_bytes(tail[i : i + _Y_BYTES], "big")
        if y >= field.FIELD_PRIME:
            raise ShareEncodingError(f"block {i // _Y_BYTES}: y value is not an element of GF(p)")
        ys.append(y)
    return Share(x=x, ys=tuple(ys)), threshold, total


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    # Strict canonical base64url: standard alphabet, '-', '_', no padding.
    # Any stray character (whitespace, '+', '/', '=', newline) is rejected so
    # that corrupted encodings are reported instead of silently normalised.
    if not text or any(ch not in _BASE64URL_ALPHABET for ch in text):
        raise ShareEncodingError("share is not canonical unpadded base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ShareEncodingError(f"invalid base64url encoding: {exc}") from exc


def encode_share(share: Share, threshold: int, total: int) -> str:
    """Encode one share (with its parameters) as a compact ``SSS1$`` string."""
    return _PREFIX + _b64url_encode(_pack(share, threshold, total))


def share_to_json_object(share: Share, threshold: int, total: int) -> dict:
    """Encode one share as the canonical JSON-serialisable object form."""
    _pack(share, threshold, total)  # validates
    return {
        "version": PARAM_VERSION,
        "x": share.x,
        "threshold": threshold,
        "total": total,
        "y": [_b64url_encode(y.to_bytes(_Y_BYTES, "big")) for y in share.ys],
    }


def _decode_json_object(obj: dict):
    if not isinstance(obj, dict):
        raise ShareEncodingError("JSON share must be an object")
    version = obj.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ShareEncodingError("missing or non-integer 'version'")
    if version != PARAM_VERSION:
        raise UnsupportedVersionError(f"share version {version} is not supported (supported: {PARAM_VERSION})")

    def _strict_int(name):
        value = obj.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ShareEncodingError(f"field '{name}' must be an integer")
        return value

    x, threshold, total = _strict_int("x"), _strict_int("threshold"), _strict_int("total")
    ys_encoded = obj.get("y")
    if not isinstance(ys_encoded, list) or not ys_encoded:
        raise ShareEncodingError("field 'y' must be a non-empty list")
    if not 2 <= threshold <= MAX_TOTAL_SHARES:
        raise ShareEncodingError("threshold out of range")
    if not threshold <= total <= MAX_TOTAL_SHARES:
        raise ShareEncodingError("total out of range")
    if not 1 <= x <= total:
        raise ShareEncodingError("share index outside 1..total")
    ys = []
    for i, encoded in enumerate(ys_encoded):
        if not isinstance(encoded, str):
            raise ShareEncodingError(f"y[{i}] must be a base64url string")
        raw_y = _b64url_decode(encoded)
        if len(raw_y) != _Y_BYTES:
            raise ShareEncodingError(f"y[{i}] must decode to exactly {_Y_BYTES} bytes")
        y = int.from_bytes(raw_y, "big")
        if y >= field.FIELD_PRIME:
            raise ShareEncodingError(f"y[{i}] is not an element of GF(p)")
        ys.append(y)
    # Reject unknown fields: a forward-compatible reader should not silently
    # drop security-relevant extensions.
    allowed = {"version", "x", "threshold", "total", "y"}
    extra = set(obj) - allowed
    if extra:
        raise UnsupportedVersionError(f"unknown fields in share: {sorted(extra)}")
    return Share(x=x, ys=tuple(ys)), threshold, total


def decode_share(encoded):
    """Decode a share from str (SSS1$ / raw base64url / JSON text), dict or bytes.

    Returns ``(Share, threshold, total)``.
    """
    if isinstance(encoded, Share):
        raise ShareEncodingError("input is already a decoded Share; pass the encoded form")
    if isinstance(encoded, dict):
        return _decode_json_object(encoded)
    if isinstance(encoded, (bytes, bytearray)):
        return _unpack(bytes(encoded))
    if isinstance(encoded, str):
        text = encoded
        if text.startswith(_PREFIX):
            return _unpack(_b64url_decode(text[len(_PREFIX) :]))
        # A JSON object encoded as text.
        if text[:1] in ("{", "["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ShareEncodingError(f"invalid JSON in share: {exc}") from exc
            return _decode_json_object(parsed)
        # Bare base64url of the binary form.
        return _unpack(_b64url_decode(text))
    raise ShareEncodingError(f"unsupported share input type: {type(encoded).__name__}")
