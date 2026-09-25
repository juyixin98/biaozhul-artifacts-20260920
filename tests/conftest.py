"""Shared test helpers."""

import base64

from threshold_shares.field import FIELD_PRIME


def flip_last_y(token):
    """Flip one bit in the final y block of an SSS1$ token; keep it < p."""
    body = token[len("SSS1$") :]
    raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    for bit in range(8):
        candidate = bytearray(raw)
        candidate[-1] ^= 1 << bit
        if int.from_bytes(candidate[-32:], "big") < FIELD_PRIME:
            return "SSS1$" + base64.urlsafe_b64encode(bytes(candidate)).rstrip(b"=").decode()
    raise AssertionError("no valid field-element bit flip available")
