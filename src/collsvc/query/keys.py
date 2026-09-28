"""Sort-key level-section mathematics.

ICU binary sort keys are laid out as level sections separated by ``0x01``
(and a trailing ``0x00`` terminator)::

    <primary weights> 0x01 <secondary weights> 0x01 <tertiary ...> 0x00

Important finding (property-tested, see
``tests/unit/test_collation_properties.py``): only the **primary** section is
a byte-wise append of per-character weights. Secondary/tertiary sections use
position-counter common weights (``05, 06, 07 ...``), so a generic "starts
with" test must NOT be implemented as byte-prefixing of those sections.

Therefore this module exposes only what is actually true:

* :func:`primary_upper_bound` — a superset interval over the primary section,
  used purely to *narrow* the SQL seek for prefix queries. It is a superset
  (and, with numeric collation or IDENTICAL strength, not even a complete
  one — the caller detects those configurations and falls back to a full
  scan, flagging the degradation in the response trace).
* :func:`bounded_interval` — value ranges use full-key BLOB comparison, which
  is always exact.
"""
from __future__ import annotations

LEVEL_SEPARATOR = 0x01
KEY_TERMINATOR = 0x00

# Two 0xFF bytes above the last primary weight. Primary continuation bytes
# never reach 0xFF, so this stays inside the next unrelated primary weight.
PRIMARY_HEADROOM = b"\xff\xff"


def split_sections(sort_key: bytes) -> tuple[bytes, ...]:
    """Split an ICU sort key into per-level sections (terminator removed)."""
    body = sort_key
    if body and body[-1] == KEY_TERMINATOR:
        body = body.rstrip(b"\x00")
    if not body:
        return (b"",)
    return tuple(body.split(bytes([LEVEL_SEPARATOR])))


def primary_section(sort_key: bytes) -> bytes:
    return split_sections(sort_key)[0]


def primary_upper_bound(sort_key: bytes) -> bytes:
    """Exclusive upper bound of the primary-section narrowing interval."""
    return primary_section(sort_key) + PRIMARY_HEADROOM


def bounded_interval(low_key: bytes, high_key: bytes) -> tuple[bytes, bytes]:
    """Normalize a between-range so ``low <= high`` at the key level."""
    return (low_key, high_key) if low_key <= high_key else (high_key, low_key)
