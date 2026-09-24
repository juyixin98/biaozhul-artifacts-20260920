"""Hand-checkable bit-layout tests.

Every nontrivial case below carries an explicit bitmap so a reviewer can
verify the bit extraction with paper and pencil -- no library needed.
"""

from __future__ import annotations

import pytest

from app.bitops import (
    INTEL,
    MOTOROLA,
    BitLayoutError,
    apply_sign,
    extract_raw,
    motorola_positions,
    intel_positions,
    render_bitmap,
    validate_within_frame,
)


# --------------------------------------------------------------------------- #
# Position walking
# --------------------------------------------------------------------------- #
def test_intel_positions_ascend():
    # LSB-first walk: S, S+1, ... in DBC sawtooth numbering
    assert intel_positions(0, 8) == [0, 1, 2, 3, 4, 5, 6, 7]
    assert intel_positions(4, 12) == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]


def test_motorola_positions_walk_down_then_jump():
    # 7|16@0 is the canonical big-endian 16-bit signal in bytes 0-1
    assert motorola_positions(7, 16) == [
        7, 6, 5, 4, 3, 2, 1, 0,       # byte 0: MSB -> LSB
        15, 14, 13, 12, 11, 10, 9, 8,  # jump +15, walk down byte 1
    ]


def test_motorola_non_byte_aligned_walk():
    # 11|12@0: 4 down-walk bits in byte 1 (11..8), then jump to 23..20
    assert motorola_positions(11, 12) == [11, 10, 9, 8, 23, 22, 21, 20,
                                          19, 18, 17, 16]


# --------------------------------------------------------------------------- #
# Intel / little-endian hand bitmaps
# --------------------------------------------------------------------------- #
def test_intel_cross_byte_hand_bitmap():
    """12-bit Intel signal 4|12@1 = raw 0x9C4 (2500), crossing two bytes.

    Intel walks DBC positions upward: signal bit k lives at position S+k.
    With start=4, signal bits 0..3 occupy the HIGH nibble of byte 0
    (positions 4..7) and bits 4..11 fill all of byte 1 (positions 8..15)::

        pos:     3 2 1 0 | 7 6 5 4 | 15 14 13 12 | 11 10 9 8
        wire:   (unused low nibble) | b3 b2 b1 b0 | b11 b10 b9 b8
        byte0:   x x x x  0  1  0  0   -> 0x40
        byte1:   1 0 0 1  1  1  0  0   -> 0x9C
                 \_ raw bits 8..11 = 0x9 _/\_ bits 4..7 = 0xC _/

    raw = 0x9 << 8 | 0xC << 4 | 0x4 = 0x9C4.
    """
    data = [0x40, 0x9C, 0, 0, 0, 0, 0, 0]
    assert extract_raw(data, 4, 12, INTEL) == 0x9C4
    assert extract_raw(data, 4, 12, INTEL) == 2500


def test_intel_full_8_bytes():
    """64-bit Intel over 8 bytes: little-endian 0x0102030405060708."""
    data = [0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01]
    assert extract_raw(data, 0, 64, INTEL) == 0x0102030405060708


def test_intel_unaligned_16bit_in_middle_bytes():
    """4|16@1 spanning bytes 0..2, raw 0xBCDE.

    Signal bits 0..3 at positions 4..7 (high nibble of byte0), bits 4..11
    at positions 8..15 (whole byte1), bits 12..15 at positions 16..19
    (LOW nibble of byte2)::

        byte0 high nibble = raw low nibble 0xE -> 0xE0
        byte1             = raw middle byte 0xCD
        byte2 low nibble  = raw high nibble 0xB -> 0x0B
    """
    data = [0xE0, 0xCD, 0x0B, 0, 0, 0, 0, 0]
    assert extract_raw(data, 4, 16, INTEL) == 0xBCDE


# --------------------------------------------------------------------------- #
# Motorola / big-endian hand bitmaps
# --------------------------------------------------------------------------- #
def test_motorola_cross_byte_hand_bitmap():
    """16-bit Motorola 7|16@0 = raw 0x1234.

        pos:   7 6 5 4 3 2 1 0 | 15 14 13 12 11 10 9 8
        byte0: 0 0 0 1 0 0 1 0 |           -> 0x12
        byte1:                    0 0 1 1 0 1 0 0 -> 0x34
        signal value on the wire IS 0x12 0x34 in order.
    """
    data = [0x12, 0x34, 0, 0, 0, 0, 0, 0]
    assert extract_raw(data, 7, 16, MOTOROLA) == 0x1234


def test_motorola_unaligned_12bit_hand_bitmap():
    """12-bit Motorola 11|12@0 = raw 0x4D2 (1234).

    Positions: 11,10,9,8 then the +15 jump to 23,22,21,20,19,18,17,16.
        raw bits 11..8 = 0x4 -> LOW nibble of byte1 (positions 11..8)
        raw bits  7..0 = 0xD2 -> byte2 (positions 23..16)
    Note byte1 high nibble and byte0 are not touched by this layout.
    """
    data = [0, 0x04, 0xD2, 0, 0, 0, 0, 0]
    assert extract_raw(data, 11, 12, MOTOROLA) == 0x4D2


def test_motorola_full_8_bytes():
    """64-bit Motorola 7|64@0: wire bytes are exactly the big-endian value.

    Start bit 7 (MSB of byte0); the walk consumes every byte in order.
    """
    data = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]
    assert extract_raw(data, 7, 64, MOTOROLA) == 0x0102030405060708


def test_byte_orders_must_differ_on_same_bytes():
    """Guard against implementations that ignore endianness.

    Bytes [0x12, 0x34] decode differently for Intel vs Motorola even though
    both signals cover exactly the same two bytes:
        7|16@0 -> 0x1234
        0|16@1 -> 0x3412
    """
    data = [0x12, 0x34, 0, 0, 0, 0, 0, 0]
    assert extract_raw(data, 7, 16, MOTOROLA) == 0x1234
    assert extract_raw(data, 0, 16, INTEL) == 0x3412
    assert extract_raw(data, 7, 16, MOTOROLA) != extract_raw(
        data, 0, 16, INTEL
    )


# --------------------------------------------------------------------------- #
# Signed values (two's complement)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,length,signed,expected",
    [
        (0, 8, False, 0),
        (255, 8, False, 255),
        (255, 8, True, -1),
        (216, 8, True, -40),
        (0x80, 8, True, -128),
        (0x7F, 8, True, 127),
        (0xFC18, 16, True, -1000),   # 64536 - 65536
        (0xFC18, 16, False, 64536),
        (1, 1, True, -1),            # 1-bit signed: only 0 and -1
        (0, 1, True, 0),
    ],
)
def test_sign_extension(raw, length, signed, expected):
    assert apply_sign(raw, length, signed) == expected


# --------------------------------------------------------------------------- #
# DLC / illegal layout
# --------------------------------------------------------------------------- #
def test_signal_overflowing_dlc_is_rejected():
    # 4|12@1 reaches DBC position 15 -> needs 2 bytes, DLC 1 is insufficient
    with pytest.raises(BitLayoutError, match="outside the 1-byte frame"):
        validate_within_frame(4, 12, INTEL, 1)


def test_motorola_overflowing_dlc_is_rejected():
    # 11|12@0 jumps into byte 3 -> DLC 2 (positions 0..15) is insufficient
    with pytest.raises(BitLayoutError, match="outside the 2-byte frame"):
        validate_within_frame(11, 12, MOTOROLA, 2)


def test_dlc_zero_rejects_every_signal():
    with pytest.raises(BitLayoutError):
        validate_within_frame(0, 1, INTEL, 0)


def test_render_bitmap_is_stable():
    text = render_bitmap([0x12, 0x34])
    assert "byte 0" in text and "0x12" in text and "0x34" in text
