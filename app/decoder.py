"""Offline CAN frame decoding.

The decoder works entirely from parsed :class:`~app.dbc.Database` objects
and raw byte strings — no vehicle hardware, sockets or network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .dbc import Database, Signal, signal_bit_positions
from .errors import FrameError
from .versioning import dbc_version, signal_version


@dataclass(frozen=True)
class DecodedSignal:
    name: str
    raw: int
    physical: float
    unit: str
    byte_order: str
    signed: bool
    present: bool
    mux_id: int | None
    signal_version: str


@dataclass(frozen=True)
class DecodedFrame:
    frame_id: int
    message_name: str
    dlc: int
    mux_value: int | None
    signals: tuple[DecodedSignal, ...]
    dbc_version: str


def _extract_unsigned(data: bytes, signal: Signal) -> int:
    value = 0
    for n in signal_bit_positions(signal):
        bit = (data[n // 8] >> (n % 8)) & 1
        value = (value << 1) | bit
    return value


def _to_signed(raw: int, length: int) -> int:
    if raw & (1 << (length - 1)):
        raw -= 1 << length
    return raw


def _physical(raw: int, signal: Signal) -> float:
    # Decimal keeps classic cases (e.g. 0.1 steps, -40 offsets) exact before
    # the final conversion to float for JSON output.
    result = Decimal(raw) * Decimal(str(signal.factor)) + Decimal(
        str(signal.offset)
    )
    return float(result)


def decode_frame(
    db: Database,
    frame_id: int,
    data: bytes,
    *,
    strict_dlc: bool = True,
) -> DecodedFrame:
    message = next((m for m in db.messages if m.frame_id == frame_id), None)
    if message is None:
        raise FrameError(
            f"no BO_ definition for frame id {frame_id} "
            f"(0x{frame_id:X})"
        )

    if len(data) != message.dlc and strict_dlc:
        relation = "shorter than" if len(data) < message.dlc else "longer than"
        raise FrameError(
            f"DLC mismatch for frame {message.name} (0x{frame_id:03X}): "
            f"received {len(data)} data bytes, DBC declares {message.dlc} "
            f"({relation} declared)"
        )
    if len(data) < message.dlc:
        # Only reachable with strict_dlc=False; guard all byte accesses.
        raise FrameError(
            f"frame {message.name}: {len(data)} bytes cannot satisfy a "
            f"{message.dlc}-byte definition"
        )

    by_name = {s.name: s for s in message.signals}
    mux_value: int | None = None
    if message.mux_switch is not None:
        switch = by_name[message.mux_switch]
        mux_value = _extract_unsigned(data, switch)
        defined = {s.mux_id for s in message.signals if s.mux_kind == "m"}
        if mux_value not in defined:
            raise FrameError(
                f"frame {message.name} (0x{frame_id:03X}): multiplexor "
                f"'{message.mux_switch}' value {mux_value} selects no defined "
                f"branch (defined switch values: {sorted(defined)})"
            )

    version = dbc_version(db)
    decoded: list[DecodedSignal] = []
    for sig in message.signals:
        if sig.mux_kind == "m" and sig.mux_id != mux_value:
            # Inactive multiplex branch: reported but marked absent, without
            # touching bytes that may belong to another branch.
            decoded.append(
                DecodedSignal(
                    name=sig.name,
                    raw=0,
                    physical=0.0,
                    unit=sig.unit,
                    byte_order=sig.byte_order,
                    signed=sig.signed,
                    present=False,
                    mux_id=sig.mux_id,
                    signal_version=signal_version(sig),
                )
            )
            continue

        raw_unsigned = _extract_unsigned(data, sig)
        raw = _to_signed(raw_unsigned, sig.length) if sig.signed else raw_unsigned
        decoded.append(
            DecodedSignal(
                name=sig.name,
                raw=raw,
                physical=_physical(raw, sig),
                unit=sig.unit,
                byte_order=sig.byte_order,
                signed=sig.signed,
                present=True,
                mux_id=(mux_value if sig.mux_kind == "M" else sig.mux_id),
                signal_version=signal_version(sig),
            )
        )

    return DecodedFrame(
        frame_id=frame_id,
        message_name=message.name,
        dlc=message.dlc,
        mux_value=mux_value,
        signals=tuple(decoded),
        dbc_version=version,
    )
