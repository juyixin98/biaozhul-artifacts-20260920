"""Hand-verifiable bit-map tests.

Each case here is small enough to verify on paper: the DBC bit number maps
to byte n//8 and bit n%8 (0=LSB), and the extraction order for the two
byte orders is checked explicitly.
"""

from __future__ import annotations

from app.dbc import Signal, parse_dbc, signal_bit_positions
from app.decoder import _extract_unsigned, _to_signed


def sig(name, start, length, order="intel", signed=False, mux_kind=None,
        mux_id=None):
    return Signal(
        name=name, start=start, length=length, byte_order=order,
        signed=signed, factor=1.0, offset=0.0, unit="", receivers=(),
        mux_kind=mux_kind, mux_id=mux_id,
        bits=frozenset(signal_bit_positions(
            Signal(name, start, length, order, signed, 1.0, 0.0, "", (),
                   mux_kind, mux_id))),
    )


# -- extraction order -------------------------------------------------------- #

def test_intel_walk_is_descending():
    # 16-bit Intel at start 0 occupies DBC bits 0..15; MSB-first extraction
    # order is 15,14,...,0.
    assert signal_bit_positions(sig("S", 0, 16, "intel")) == list(range(15, -1, -1))


def test_motorola_single_byte_walk():
    # Motorola start=7, length=8 -> bits 7,6,5,4,3,2,1,0 (byte 0, MSB left).
    assert signal_bit_positions(sig("S", 7, 8, "motorola")) == [7, 6, 5, 4, 3, 2, 1, 0]


def test_motorola_cross_byte_walk():
    # Classic 16-bit Motorola starting at DBC bit 7 spans bytes 0-1.
    assert signal_bit_positions(sig("S", 7, 16, "motorola")) == [
        7, 6, 5, 4, 3, 2, 1, 0, 15, 14, 13, 12, 11, 10, 9, 8
    ]


def test_motorola_wrap_walk_start_0():
    # Start at byte0 bit0, 8 bits: 0 then byte1 column7..0.
    assert signal_bit_positions(sig("S", 0, 8, "motorola")) == [
        0, 15, 14, 13, 12, 11, 10, 9
    ]


def test_motorola_wrap_walk_start_12():
    assert signal_bit_positions(sig("S", 12, 9, "motorola")) == [
        12, 11, 10, 9, 8, 23, 22, 21, 20
    ]


# -- hand-computed extractions ---------------------------------------------- #

def test_intel_cross_byte_value():
    # bytes: [0x28, 0x0A] -> little-endian uint16 = 0x0A28 = 2600
    data = bytes.fromhex("280A") + b"\x00" * 6
    assert _extract_unsigned(data, sig("S", 0, 16, "intel")) == 0x0A28


def test_motorola_cross_byte_value():
    # bytes: [0x12,0x34] 16-bit big-endian = 0x1234 = 4660
    data = bytes.fromhex("1234") + b"\x00" * 6
    assert _extract_unsigned(data, sig("S", 7, 16, "motorola")) == 0x1234


def test_signed_negative():
    # 0xE0 in an 8-bit signed field = -32
    raw = _extract_unsigned(b"\x00" * 2 + bytes([0xE0]) + b"\x00" * 5,
                            sig("T", 16, 8, "intel"))
    assert raw == 0xE0
    assert _to_signed(raw, 8) == -32


def test_signed_negative_32bit_motorola():
    # 0xFFFFFFFF, 32-bit signed -> -1 (start=7 spans bytes 0..3)
    data = bytes.fromhex("FFFFFFFF00000000")
    raw = _extract_unsigned(data, sig("B", 7, 32, "motorola"))
    assert raw == 0xFFFFFFFF
    assert _to_signed(raw, 32) == -1


def test_signed_positive_high_bit_not_set():
    assert _to_signed(0x7F, 8) == 127
    assert _to_signed(0x80, 9) == 128  # in 9 bits this is positive


# -- bitmap renderer is readable -------------------------------------------- #

def test_bitmap_render_from_example(tmp_path):
    from pathlib import Path
    from app.bitmap import render_message_bitmap

    root = Path(__file__).resolve().parents[1]
    db = parse_dbc((root / "examples" / "powertrain.dbc").read_text())
    engine = next(m for m in db.messages if m.frame_id == 0x100)
    text = render_message_bitmap(engine)
    # Header and every declared byte are present.
    assert "Frame 0x100" in text
    assert "ENGINE_STATUS" in text
    for byte_index in range(8):
        assert f"byte{byte_index}" in text
    # Legend names each signal with its DBC coordinates.
    assert "EngineSpeed start=0 len=16 intel" in text
    assert "EngineTemp start=16 len=8 intel signed" in text

    diag = next(m for m in db.messages if m.frame_id == 0x400)
    text = render_message_bitmap(diag)
    assert "[MUX switch]" in text
    assert "[mux=0]" in text and "[mux=1]" in text
