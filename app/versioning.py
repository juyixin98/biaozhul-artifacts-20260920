"""Definition-version hashing.

Two levels of version are reported with every decoded signal:

* ``signal_version`` — sha256 over the canonical definition of one signal,
  so callers can detect any change to start/length/order/sign/scaling/mux.
* ``dbc_version``    — sha256 over the canonical content of the whole DBC
  (all messages and signals), independent of comments or source formatting.

Both are deterministic, plain hex digests, and computed from the parsed
model rather than the raw text, so whitespace/comment differences do not
bump the version while real definition changes do.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from .dbc import Database, Message, Signal


def _num(value: float) -> str:
    """Stable string form for factor/offset/friends."""
    # Decimal normalises 1, 1.0, 1e0 etc.; strip a trailing exponent noise.
    d = Decimal(str(value)).normalize()
    return format(d, "f")


def _canonical_signal(s: Signal) -> str:
    parts = [
        f"name={s.name}",
        f"start={s.start}",
        f"length={s.length}",
        f"order={s.byte_order}",
        f"signed={int(s.signed)}",
        f"factor={_num(s.factor)}",
        f"offset={_num(s.offset)}",
        f"unit={s.unit}",
        f"mux={s.mux_kind if s.mux_kind in (None, 'M') else s.mux_id}",
    ]
    return "|".join(parts)


def signal_version(s: Signal) -> str:
    return hashlib.sha256(_canonical_signal(s).encode("utf-8")).hexdigest()


def _canonical_message(m: Message) -> str:
    head = (
        f"BO id={m.frame_id}|name={m.name}|dlc={m.dlc}|sender={m.sender}"
    )
    lines = [head]
    for s in m.signals:
        lines.append("  SG " + _canonical_signal(s))
    return "\n".join(lines)


def dbc_version(db: Database) -> str:
    parts = [f"VERSION={db.version}"]
    for m in sorted(db.messages, key=lambda x: x.frame_id):
        parts.append(_canonical_message(m))
    blob = "\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
