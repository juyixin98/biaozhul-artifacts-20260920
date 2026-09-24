"""Real cryptographic operations for the fusion service.

* Per-session symmetric keys generated with :mod:`secrets` (CSPRNG).
* Request authentication with HMAC-SHA256 (:func:`hmac_sign` /
  :func:`hmac_verify`, constant-time comparison).
* SHA-256 hash chaining over filter steps lives in :mod:`ekf_fusion.engine`.

The signed string is canonical, line-free text so a client in any language
can reproduce it exactly::

    v1\\n<message_id>\\n<t>\\n<kind>\\n<z0>,<z1>\\n<r00>,<r01>,<r10>,<r11>
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SIG_VERSION = "v1"


def new_session_key() -> str:
    """Return a fresh 256-bit HMAC key encoded as lowercase hex."""
    return secrets.token_hex(32)


def new_session_id() -> str:
    """Return a fresh 128-bit random session identifier."""
    return secrets.token_hex(16)


def canonical_message(
    message_id: str,
    t: float,
    kind: str,
    z: tuple[float, float] | list[float],
    r: tuple[tuple[float, float], tuple[float, float]] | list[list[float]],
) -> bytes:
    """Build the exact byte string covered by the HMAC signature."""
    line = "\n".join([
        SIG_VERSION,
        str(message_id),
        repr(float(t)),
        str(kind),
        f"{float(z[0])!r},{float(z[1])!r}",
        ",".join(repr(float(v)) for v in (r[0][0], r[0][1], r[1][0], r[1][1])),
    ])
    return line.encode("utf-8")


def hmac_sign(key_hex: str, payload: bytes) -> str:
    """Return the hex HMAC-SHA256 of ``payload`` under ``key_hex``."""
    return hmac.new(bytes.fromhex(key_hex), payload, hashlib.sha256).hexdigest()


def hmac_verify(key_hex: str, payload: bytes, signature_hex: str) -> bool:
    """Constant-time verification of a hex HMAC-SHA256 signature."""
    if not isinstance(signature_hex, str) or len(signature_hex) != 64:
        return False
    try:
        sig = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    expected = hmac.new(bytes.fromhex(key_hex), payload, hashlib.sha256).digest()
    return hmac.compare_digest(expected, sig)


def sha256_file(path: str) -> str:
    """SHA-256 of a file, streamed (used by the demo client)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
