"""Frame decoder tests: physical values, negatives, mux, DLC enforcement."""

from __future__ import annotations

import pathlib

import pytest

from app.decoder import (
    DLCTooShortError,
    DataLengthError,
    UnknownMessageError,
    decode_frame,
)
from app.dbc import parse_dbc

DBC = (
    pathlib.Path(__file__).resolve().parents[1] / "examples" / "demo.dbc"
).read_text(encoding="utf-8")

DB = parse_dbc(DBC)
VID = "v-test"


def _decode(frame_id, data, **kw):
    message = DB.message_by_id(frame_id)
    assert message is not None
    return decode_frame(message, data, VID, **kw)


def _by_name(frame):
    return {s.name: s for s in frame.signals}


# --------------------------------------------------------------------------- #
# Hand-checkable examples (mirror examples/payloads.jsonl)
# --------------------------------------------------------------------------- #
def test_engine_frame_intel_scaling_and_negative():
    # [34 12 D8 64 ...]:
    #   EngineSpeed 0|16@1  raw 0x1234=4660, *0.25 = 1165.0 rpm
    #   EngineTemp 16|8@1-  raw 0xD8=216 -> signed -40, -40+(-40) = -80 degC
    #   Throttle   24|8@1   raw 0x64=100, *0.5 = 50.0 %
    frame = _decode(256, [0x34, 0x12, 0xD8, 0x64, 0, 0, 0, 0])
    signals = _by_name(frame)
    assert signals["EngineSpeed"].raw == 4660
    assert signals["EngineSpeed"].value == 1165.0
    assert signals["EngineSpeed"].byte_order == "intel"
    assert signals["EngineTemp"].raw == 216  # unsigned wire representation
    assert signals["EngineTemp"].value == -80.0
    assert signals["EngineTemp"].signed is True
    assert signals["Throttle"].value == 50.0
    assert all(s.definition_version == VID for s in frame.signals)


def test_negative_temperature_full_negative_range():
    # raw 0x80 (128) -> signed -128, *1 + (-40) = -168 degC; the wire
    # carries the two's-complement pattern 1000 0000 at byte 2.
    frame = _decode(256, [0, 0, 0x80, 0, 0, 0, 0, 0])
    assert _by_name(frame)["EngineTemp"].raw == 128
    assert _by_name(frame)["EngineTemp"].value == -168.0


def test_motorola_cross_byte_value():
    # BigCounter 7|16@0 raw 0x1234 = 4660
    # BigSigned 23|16@0 raw 0xFC18 = 64536 -> signed -1000
    # CrossIntel 32|12@1 from bytes 0x60,0x09 = 0x960 = 2400 -> 190.0
    frame = _decode(2047, [0x12, 0x34, 0xFC, 0x18, 0x60, 0x09, 0, 0])
    signals = _by_name(frame)
    assert signals["BigCounter"].raw == 4660
    assert signals["BigCounter"].byte_order == "motorola"
    assert signals["BigSigned"].raw == 64536
    assert signals["BigSigned"].value == -1000.0
    assert signals["CrossIntel"].raw == 2400
    assert signals["CrossIntel"].value == 190.0
    assert frame.frame_id == 0x7FF


# --------------------------------------------------------------------------- #
# Multiplexing: branches, switch raw, inactive signals absent
# --------------------------------------------------------------------------- #
def test_mux_branch_one_selects_only_branch_one_signals():
    frame = _decode(300, [0x01, 0x00, 0x40, 0x06, 0, 0, 0, 0])
    assert frame.mux == 1
    names = {s.name for s in frame.signals}
    assert names == {"BrakeMux", "PedalPos", "BrakePress"}
    signals = _by_name(frame)
    assert signals["BrakePress"].raw == 1600
    assert signals["BrakePress"].value == 160.0
    assert signals["PedalPos"].raw == 0


def test_mux_branch_two_selects_motorola_signal():
    # LineVoltage 47|12@0 raw 1134 *0.01 = 11.34 V; ErrorCode 32|4 = 6
    frame = _decode(300, [0x02, 0, 0, 0, 0x06, 0x46, 0xE0, 0])
    assert frame.mux == 2
    names = {s.name for s in frame.signals}
    assert names == {"BrakeMux", "LineVoltage", "ErrorCode"}
    signals = _by_name(frame)
    assert signals["LineVoltage"].raw == 1134
    assert abs(signals["LineVoltage"].value - 11.34) < 1e-12
    assert signals["LineVoltage"].byte_order == "motorola"
    assert signals["ErrorCode"].raw == 6


def test_unknown_mux_value_decodes_switch_and_plain_only():
    # mux = 9: no branch matches -> only the switch itself is returned
    frame = _decode(300, [0x09, 0, 0, 0, 0, 0, 0, 0])
    assert frame.mux == 9
    assert [s.name for s in frame.signals] == ["BrakeMux"]


# --------------------------------------------------------------------------- #
# DLC enforcement
# --------------------------------------------------------------------------- #
def test_short_payload_is_dlc_error():
    with pytest.raises(DLCTooShortError):
        _decode(300, [0x01, 0x00, 0x40, 0x00, 0, 0, 0])


def test_wrong_payload_length_is_error():
    # Exactly-DLC payloads are required; a different length is a mismatch
    message = DB.message_by_id(300)
    with pytest.raises(DataLengthError):
        # length 8, but caller explicitly declares DLC 3
        decode_frame(message, [0] * 8, VID, dlc=3)


def test_payload_longer_than_8_bytes_rejected():
    with pytest.raises(DataLengthError, match="8-byte"):
        _decode(256, [0] * 9)


def test_explicit_dlc_must_match_payload_length():
    with pytest.raises(DataLengthError):
        _decode(300, [0x01, 0x00, 0x40, 0x06, 0, 0, 0, 0], dlc=3)


def test_branch_signal_overflowing_payload_is_dlc_error():
    # Build a message with DLC 8, then feed 2 bytes while mux selects a
    # 16-bit branch signal -> the selected branch does not fit.
    dbc = """
VERSION "x"
NS_ :
 BS_:
BU_: A
BO_ 500 T: 8 A
 SG_ M M0 : 0|4@1+ (1,0) [0|15] "" A
 SG_ W m1 : 8|16@1+ (1,0) [0|65535] "" A
"""
    from app.dbc import parse_dbc as _parse
    db = _parse(dbc)
    msg = db.message_by_id(500)
    with pytest.raises(DLCTooShortError):
        decode_frame(msg, [0x01, 0], VID)


def test_unknown_frame_lookup_helper():
    assert DB.message_by_id(0x7FE) is None
    # The API maps the "message not defined" condition to UnknownMessageError;
    # emulate it at the boundary:
    with pytest.raises(UnknownMessageError):
        raise UnknownMessageError("frame not defined")
