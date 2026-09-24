r"""Bit-level extraction for CAN signals.

DBC bit numbering ("sawtooth" / start-bit numbering)
----------------------------------------------------

Bits inside byte ``b`` (b = 0 is the first byte on the wire) are numbered
``8*b + i`` where ``i`` is the bit position inside the byte counted from the
least significant bit (``i = 0`` is the LSB, ``i = 7`` is the MSB)::

              bit index inside one byte (i)
        7   6   5   4   3   2   1   0
      +---+---+---+---+---+---+---+---+
      |MSB|   |   |   |   |   |   |LSB|
      +---+---+---+---+---+---+---+---+
    DBC positions of byte b: 8b+7 ... 8b+0

Byte orders (this difference MUST be honored, never collapsed):

* Intel / little-endian (``@1``): the DBC start bit is the LSB. Subsequent
  bits walk toward higher DBC positions: S, S+1, S+2, ...
* Motorola / big-endian (``@0``): the DBC start bit is the MSB. Inside a byte
  bits walk DOWN toward index 0, then the walk JUMPS to the MSB (index 7) of
  the next byte: S, S-1, ... , S-k reaching position 8*b, then the next
  position is 8*(b+1)+7 (= previous position + 15).

Example hand-checkable Motorola layout, start bit 7, length 16 (``7|16@0``),
raw value 0x1234 in an 8-byte frame::

    DBC pos:  7  6  5  4  3  2  1  0 | 15 14 13 12 11 10  9  8 | ...
    bit:      0  0  0  1  0  0  1  0 |  0  0  1  1  0  1  0  0 | ...
    value:    \______ 0x12 _______/    \_______ 0x34 ________/
    on wire (byte0 / byte1):           0x12          0x34

Everything here operates on pure Python integers; no third-party library is
involved, so the behaviour is fully auditable and is cross-checked against
``cantools`` in the test-suite.
"""

from __future__ import annotations

from collections.abc import Sequence

INTEL = "intel"
MOTOROLA = "motorola"

BYTE_ORDER_BY_DBC_CODE = {
    "1": INTEL,
    "0": MOTOROLA,
}


class BitLayoutError(ValueError):
    """Raised for illegal signal widths or signals exceeding the frame DLC."""


def intel_positions(start: int, length: int) -> list[int]:
    """DBC positions of an Intel signal, ordered LSB-first (bit 0 first)."""

    return [start + k for k in range(length)]


def motorola_positions(start: int, length: int) -> list[int]:
    """DBC positions of a Motorola signal, ordered MSB-first.

    Walk down inside the current byte; after reaching index 0 jump to the
    MSB (index 7) of the next byte, i.e. +15 from the consumed index-0 bit.
    """

    positions: list[int] = []
    position = start
    for _ in range(length):
        positions.append(position)
        if position % 8 == 0:
            position += 15  # jump: byte b, index 0 -> byte b+1, index 7
        else:
            position -= 1
    return positions


def positions_for(start: int, length: int, byte_order: str) -> list[int]:
    if length < 1:
        raise BitLayoutError("signal length must be >= 1 bit")
    if start < 0:
        raise BitLayoutError("start bit must be >= 0")
    if byte_order == INTEL:
        return intel_positions(start, length)
    if byte_order == MOTOROLA:
        return motorola_positions(start, length)
    raise BitLayoutError(f"unknown byte order: {byte_order!r}")


def validate_within_frame(
    start: int, length: int, byte_order: str, dlc: int
) -> list[int]:
    """Return the occupied positions or raise if the signal does not fit.

    ``dlc`` is the message DLC in bytes (0..8).
    """

    if not 0 <= dlc <= 8:
        raise BitLayoutError(f"DLC must be in 0..8 bytes, got {dlc}")
    positions = positions_for(start, length, byte_order)
    frame_bits = 8 * dlc
    bad = [p for p in positions if p < 0 or p >= frame_bits]
    if bad:
        raise BitLayoutError(
            f"signal at {start}|{length} ({byte_order}) occupies DBC bit "
            f"position(s) outside the {dlc}-byte frame "
            f"(valid positions 0..{frame_bits - 1 if frame_bits else -1}); "
            f"offending positions: {sorted(set(bad))}"
        )
    return positions


def _bit_at(data: Sequence[int], position: int) -> int:
    byte_index, bit_index = divmod(position, 8)
    return (data[byte_index] >> bit_index) & 1


def extract_raw(
    data: Sequence[int],
    start: int,
    length: int,
    byte_order: str,
) -> int:
    """Extract the *raw* (unscaled, unsigned representation) signal integer.

    Caller is responsible for having validated the layout against the DLC.
    """

    if byte_order == INTEL:
        # positions are ordered LSB-first: position of signal bit k is S+k
        raw = 0
        for k, position in enumerate(intel_positions(start, length)):
            raw |= _bit_at(data, position) << k
        return raw

    # Motorola: positions ordered MSB-first, length k counts down from MSB
    positions = motorola_positions(start, length)
    raw = 0
    for k, position in enumerate(positions):
        raw |= _bit_at(data, position) << (length - 1 - k)
    return raw


def apply_sign(raw: int, length: int, signed: bool) -> int:
    """Apply two's-complement sign extension for a signed signal."""

    if signed and raw >= (1 << (length - 1)):
        return raw - (1 << length)
    return raw


def render_bitmap(data: Sequence[int]) -> str:
    """Render data bytes as DBC-positioned bits (handy for failing tests).

    Output for ``bytes([0x12, 0x34])``::

        byte 0: DBC  7  6  5  4  3  2  1  0
                bits 0  0  0  1  0  0  1  0   (0x12)
        byte 1: DBC 15 14 13 12 11 10  9  8
                bits 0  0  1  1  0  1  0  0   (0x34)
    """

    lines: list[str] = []
    for byte_index, value in enumerate(data):
        base = 8 * byte_index
        pos_header = " ".join(f"{base + i:3d}" for i in range(7, -1, -1))
        bit_row = " ".join(f"  {(value >> i) & 1}" for i in range(7, -1, -1))
        lines.append(f"byte {byte_index}: DBC {pos_header}")
        lines.append(f"         bits{bit_row}   (0x{value:02X})")
    return "\n".join(lines)
