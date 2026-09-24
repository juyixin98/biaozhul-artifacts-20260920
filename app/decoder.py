"""Frame decoding: raw integers, sign extension, scaling and mux selection."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from collections.abc import Sequence

from .bitops import apply_sign, extract_raw, validate_within_frame
from .dbc import Message, Signal


class DecodeError(ValueError):
    """Raised when a frame cannot be decoded against its DBC definition."""

    code = "decode_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message}


class DLCTooShortError(DecodeError):
    code = "dlc_too_short"


class DataLengthError(DecodeError):
    code = "data_length_mismatch"


class UnknownMessageError(DecodeError):
    code = "unknown_message"


@dataclass(frozen=True)
class DecodedSignal:
    name: str
    raw: int
    value: float
    unit: str
    length: int
    byte_order: str
    signed: bool
    mux_kind: str
    mux_value: int | None
    definition_version: str


@dataclass(frozen=True)
class DecodedFrame:
    frame_id: int
    message_name: str
    dlc: int
    data_hex: str
    definition_version: str
    mux: int | None
    signals: tuple[DecodedSignal, ...]


def physical(raw_signed: int, signal: Signal) -> Decimal:
    return signal.factor * Decimal(raw_signed) + signal.offset


def _to_float(value: Decimal) -> float:
    # Decimal -> float is the only place floating point is introduced;
    # hand-checkable examples use integer/half factors so the result is exact.
    return float(value)


def decode_frame(
    message: Message,
    data: Sequence[int],
    definition_version: str,
    *,
    dlc: int | None = None,
) -> DecodedFrame:
    """Decode one data field against one message definition.

    ``data`` is the full wire payload (0..8 bytes). ``dlc`` may override the
    expected DLC; by default the DBC DLC must match the payload length
    exactly. Short payloads are reported as DLC errors, never padded.
    """

    actual_dlc = len(data)
    if any(not 0 <= byte <= 0xFF for byte in data):
        raise DataLengthError("each data byte must be in range 0..255")
    if actual_dlc > 8:
        raise DataLengthError(
            f"payload of {actual_dlc} bytes exceeds the 8-byte CAN maximum"
        )

    # A frame shorter than the DBC definition is a CAN DLC error first and
    # foremost (signals may not fit); never silently pad such a payload.
    if actual_dlc < message.dlc:
        raise DLCTooShortError(
            f"message {message.name!r} requires DLC {message.dlc} but "
            f"payload only contains {actual_dlc} byte(s)"
        )

    expected_dlc = message.dlc if dlc is None else dlc
    if dlc is not None and not 0 <= dlc <= 8:
        raise DLCTooShortError(f"explicit DLC {dlc} outside 0..8")
    if expected_dlc != actual_dlc:
        raise DataLengthError(
            f"payload length {actual_dlc} does not match declared DLC "
            f"{expected_dlc} for message {message.name!r}"
        )

    # Validate every signal against the payload first: an undersized frame
    # for an otherwise selected branch is rejected as a DLC error.
    mux_value: int | None = None
    switch = message.mux_switch
    if switch is not None:
        try:
            validate_within_frame(
                switch.start, switch.length, switch.byte_order, actual_dlc
            )
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise DLCTooShortError(str(exc)) from exc
        raw_switch = extract_raw(
            data, switch.start, switch.length, switch.byte_order
        )
        mux_value = apply_sign(raw_switch, switch.length, switch.signed)

    selected: list[Signal] = []
    for signal in message.signals:
        if signal.mux_kind == "branch" and signal.mux_value != mux_value:
            continue
        try:
            validate_within_frame(
                signal.start, signal.length, signal.byte_order, actual_dlc
            )
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise DLCTooShortError(str(exc)) from exc
        selected.append(signal)

    decoded: list[DecodedSignal] = []
    for signal in selected:
        raw = extract_raw(
            data, signal.start, signal.length, signal.byte_order
        )
        signed_raw = apply_sign(raw, signal.length, signal.signed)
        value = _to_float(physical(signed_raw, signal))
        decoded.append(
            DecodedSignal(
                name=signal.name,
                raw=raw,
                value=value,
                unit=signal.unit,
                length=signal.length,
                byte_order=signal.byte_order,
                signed=signal.signed,
                mux_kind=signal.mux_kind,
                mux_value=signal.mux_value,
                definition_version=definition_version,
            )
        )

    return DecodedFrame(
        frame_id=message.frame_id,
        message_name=message.name,
        dlc=actual_dlc,
        data_hex=bytes(data).hex(" ").upper(),
        definition_version=definition_version,
        mux=mux_value,
        signals=tuple(decoded),
    )
