"""Frame decoder tests: scaling, signed values, multiplex branches, DLC."""

from __future__ import annotations

import pytest

from app.dbc import parse_dbc
from app.decoder import decode_frame
from app.errors import FrameError

HEADER = 'VERSION "x"\n\nNS_ :\n\tNS_DESC_\n\nBS_:\n\nBU_: A B\n\n'


def make_dbc():
    dbc = HEADER + (
        "BO_ 256 M: 8 A\n"
        ' SG_ RPM : 0|16@1+ (0.25,0) [0|0] "rpm" A\n'
        ' SG_ TEMP : 16|8@1- (1,-40) [0|0] "degC" A\n'
        "BO_ 512 N: 8 B\n"
        ' SG_ SPD : 7|16@0+ (0.01,0) [0|0] "km/h" A\n'
        ' SG_ LEV : 23|4@0- (1,0) [0|0] "" A\n'
        "BO_ 1024 X: 8 A\n"
        ' SG_ ID M : 0|8@1+ (1,0) [0|0] "" A\n'
        ' SG_ V0 m0 : 8|16@1+ (0.001,0) [0|0] "V" A\n'
        ' SG_ P1 m1 : 8|16@1+ (0.1,0) [0|0] "kPa" A\n'
        "BO_ 2047 Z: 8 A\n"
        ' SG_ BIG : 7|32@0- (1,0) [0|0] "" A\n'
    )
    return parse_dbc(dbc)


def by_name(frame, name):
    return next(s for s in frame.signals if s.name == name)


def test_intel_scaled_and_signed():
    db = make_dbc()
    data = bytes.fromhex("280AE00000000000")
    frame = decode_frame(db, 256, data)
    assert by_name(frame, "RPM").raw == 0x0A28
    assert by_name(frame, "RPM").physical == pytest.approx(650.0)
    temp = by_name(frame, "TEMP")
    assert temp.raw == -32
    assert temp.physical == -72.0


def test_motorola_cross_byte_and_signed_nibble():
    db = make_dbc()
    data = bytes.fromhex("1234F00000000000")
    frame = decode_frame(db, 512, data)
    spd = by_name(frame, "SPD")
    assert spd.raw == 0x1234
    assert spd.physical == pytest.approx(46.60)
    lev = by_name(frame, "LEV")
    assert lev.raw == -1


def test_mux_branch_selection():
    db = make_dbc()
    # Switch = 0 -> V0 active, P1 absent
    f0 = decode_frame(db, 1024, bytes.fromhex("0084030000000000"))
    assert f0.mux_value == 0
    v0 = by_name(f0, "V0")
    p1 = by_name(f0, "P1")
    assert v0.present and v0.raw == 0x0384
    assert v0.physical == pytest.approx(0.900)
    assert not p1.present

    f1 = decode_frame(db, 1024, bytes.fromhex("0138040000000000"))
    assert f1.mux_value == 1
    assert by_name(f1, "P1").present
    assert by_name(f1, "P1").raw == 0x0438
    assert not by_name(f1, "V0").present


def test_mux_switch_always_present():
    db = make_dbc()
    frame = decode_frame(db, 1024, bytes(8))
    switch = by_name(frame, "ID")
    assert switch.present and switch.raw == 0


def test_max_frame_id_and_negative_32bit():
    db = make_dbc()
    frame = decode_frame(db, 2047, bytes.fromhex("FFFFFFFF00000000"))
    big = by_name(frame, "BIG")
    assert big.raw == -1
    assert big.physical == -1.0


def test_dlc_too_short_rejected():
    db = make_dbc()
    with pytest.raises(FrameError, match="shorter than"):
        decode_frame(db, 256, bytes.fromhex("280A"))


def test_dlc_too_long_rejected():
    db = make_dbc()
    with pytest.raises(FrameError, match="longer than"):
        decode_frame(db, 256, bytes(9))


def test_unknown_frame_id_rejected():
    db = make_dbc()
    with pytest.raises(FrameError, match="no BO_ definition"):
        decode_frame(db, 999, bytes(8))


def test_undefined_mux_value_rejected():
    db = make_dbc()
    # switch value 9 has no branch definition
    with pytest.raises(FrameError, match="selects no defined branch"):
        decode_frame(db, 1024, bytes.fromhex("0900000000000000"))


def test_dbc_version_changes_with_definition():
    db = make_dbc()
    frame = decode_frame(db, 256, bytes(8))
    v1 = frame.dbc_version
    rpm = by_name(frame, "RPM")
    assert len(rpm.signal_version) == 64  # sha256 hex

    db2 = make_dbc()
    # mutate scale by rebuilding from text
    # (parsed objects are frozen; simulate by direct parse of altered DBC)
    from app.dbc import parse_dbc as p
    text = (HEADER +
        "BO_ 256 M: 8 A\n"
        ' SG_ RPM : 0|16@1+ (0.5,0) [0|0] "rpm" A\n')
    db2 = p(text)
    f2 = decode_frame(db2, 256, bytes(8))
    assert f2.dbc_version != v1
    assert by_name(f2, "RPM").signal_version != rpm.signal_version
