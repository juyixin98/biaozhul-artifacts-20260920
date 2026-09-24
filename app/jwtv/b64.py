"""Unpadded base64url helpers (RFC 7515, appendix C)."""

from __future__ import annotations

import base64
import re

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(segment: str) -> bytes:
    """Decode a base64url string.

    Raises ``ValueError`` for characters outside the base64url alphabet;
    missing ``=`` padding is tolerated.
    """
    if not segment or not _B64URL_RE.fullmatch(segment):
        raise ValueError("segment is not valid base64url")
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def b64url_encode_uint(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return b64url_encode(value.to_bytes(length, "big"))


def b64url_decode_uint(segment: str) -> int:
    return int.from_bytes(b64url_decode(segment), "big")
