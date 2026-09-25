"""Canonical request encoding and HMAC signing primitives.

The signed string is a newline-joined sequence of:

    METHOD
    canonical path
    canonical query
    hex(sha256(body))
    kid
    timestamp (unix seconds)
    nonce

Only HMAC-SHA256 from the Python standard library (OpenSSL backed) is used.
No custom cryptographic primitives are implemented here.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import urllib.parse

SIGNING_ALGORITHM = "HMAC-SHA256"

_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
# RFC 3986 unreserved characters; everything else in a path/query token is
# percent-encoded after decoding, so differing encodings converge.
_UNRESERVED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_SUB_DELIMS = set("!$&'()*+,;=")  # kept literal in path segments
_PATH_SAFE = _UNRESERVED | _SUB_DELIMS | {":"}
_QUERY_SAFE = _UNRESERVED | _SUB_DELIMS | {":"}


class CanonicalizationError(ValueError):
    """Raised when a path or query string cannot be decoded unambiguously."""


def _reject_bad_pct(token: str) -> None:
    """Reject stray '%' not followed by two hex digits."""
    i = 0
    while True:
        j = token.find("%", i)
        if j == -1:
            return
        if j + 2 >= len(token) or not re.fullmatch(
            r"[0-9A-Fa-f]{2}", token[j + 1 : j + 3]
        ):
            raise CanonicalizationError(
                f"malformed percent-encoding at position {j}"
            )
        i = j + 3


def _decode_strict(token: str) -> str:
    """Percent-decode a token, rejecting malformed escapes and control chars."""
    _reject_bad_pct(token)
    decoded = urllib.parse.unquote(token, encoding="utf-8", errors="strict")
    for ch in decoded:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise CanonicalizationError("control character not allowed")
    return decoded


def _re_encode(text: str, safe: set[str]) -> str:
    out = []
    for ch in text.encode("utf-8"):
        c = chr(ch)
        if c in safe:
            out.append(c)
        else:
            out.append(f"%{ch:02X}")
    return "".join(out)


def canonical_path(path: str) -> str:
    """Return the canonical form of a request path.

    The raw path is split on literal slashes *before* decoding, so an encoded
    slash (%2F) stays inside one segment and can never be confused with a path
    separator. Each segment is decoded under strict rules and re-encoded with
    RFC 3986 uppercase escapes; dot segments are then resolved.
    """
    if not path:
        raise CanonicalizationError("empty path")
    if "?" in path or "#" in path:
        raise CanonicalizationError("path must not contain query or fragment")

    raw_segments = path.split("/")
    segments: list[str] = []
    for seg in raw_segments:
        name = _re_encode(_decode_strict(seg), _PATH_SAFE)
        # Dot segments are compared on the raw encoded form; decoded "."/".."
        # are indistinguishable separators and are normalized below.
        segments.append(name)

    # Resolve "." and ".." per RFC 3986 section 5.2.4 (remove_dot_segments),
    # operating on the now canonical segments.
    leading_slash = path.startswith("/")
    trailing_slash = path.endswith("/") and len(path) > 1
    stack: list[str] = []
    for seg in segments:
        if seg == ".":
            continue
        if seg == "..":
            if stack and stack[-1] not in ("", ".."):
                stack.pop()
            elif not leading_slash:
                stack.append("..")
            continue
        stack.append(seg)

    result = "/".join(stack) if stack else ("" if leading_slash else "")
    if leading_slash and not result.startswith("/"):
        result = "/" + result
    if trailing_slash and not result.endswith("/"):
        result += "/"
    if not result:
        result = "/" if leading_slash else "."
    return result


def canonical_query(query: str) -> str:
    """Canonicalize a query string (without the leading '?').

    Keys/values are decoded and re-encoded, pairs are sorted by (key, value).
    An empty query canonicalizes to the empty string.
    """
    if query == "":
        return ""
    pairs = []
    for part in query.split("&"):
        if part == "":
            raise CanonicalizationError("empty query parameter")
        key, sep, value = part.partition("=")
        k = _re_encode(_decode_strict(key), _QUERY_SAFE)
        v = _re_encode(_decode_strict(value), _QUERY_SAFE) if sep else ""
        pairs.append((k, v))
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


def body_digest(body: bytes) -> str:
    """Hex SHA-256 of the raw body; empty body is the hash of b''."""
    return hashlib.sha256(body or b"").hexdigest()


def canonical_request(
    method: str,
    target: str,
    body: bytes,
    *,
    key_id: str,
    timestamp: int | str,
    nonce: str,
) -> str:
    """Build the exact newline-joined string that gets signed.

    ``target`` is the raw request target (path, optionally '?' + query) as
    received on the wire, e.g. "/v1/verify?x=1".
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")
    path, sep, query = target.partition("?")
    cpath = canonical_path(path)
    cquery = canonical_query(query) if sep else ""
    lines = [
        method.upper(),
        cpath,
        cquery,
        body_digest(bytes(body)),
        key_id,
        str(int(timestamp)),
        nonce,
    ]
    return "\n".join(lines)


def sign_request(secret: bytes, canonical: str) -> str:
    """Return lowercase hex HMAC-SHA256 of the canonical request."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, canonical: str, signature: str) -> bool:
    """Constant-time signature check. Never raises on bad input."""
    try:
        expected = sign_request(secret, canonical)
        return hmac.compare_digest(expected, signature.lower())
    except (TypeError, ValueError, AttributeError):
        return False
