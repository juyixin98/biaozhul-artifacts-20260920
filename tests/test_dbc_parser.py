"""DBC parser and semantic validation tests."""

from __future__ import annotations

import pytest

from app.dbc import parse_dbc
from app.errors import DBCParseError

HEADER = 'VERSION "x"\n\nNS_ :\n\tNS_DESC_\n\nBS_:\n\nBU_: A B\n\n'


def bo(frame_id, name, dlc, sender="A"):
    return f"BO_ {frame_id} {name}: {dlc} {sender}\n"


def sg(name, spec, rest='(1,0) [0|0] "" A', mux=None):
    marker = f" {mux}" if mux is not None else ""
    return f' SG_ {name}{marker} : {spec} {rest}\n'


def build(*lines):
    return HEADER + "".join(lines)


# -- happy path -------------------------------------------------------------- #

def test_parse_minimal_message():
    db = parse_dbc(build(bo(100, "M", 1), sg("S", "0|8@1+")))
    assert len(db.messages) == 1
    m = db.messages[0]
    assert m.frame_id == 100 and m.dlc == 1
    s = m.signals[0]
    assert s.start == 0 and s.length == 8
    assert s.byte_order == "intel" and not s.signed


def test_parse_motorola_signed_scaled():
    db = parse_dbc(build(
        bo(200, "M", 8),
        sg("T", '7|16@0-', '(-0.5,40) [-40|215] "degC" B'),
    ))
    s = db.messages[0].signals[0]
    assert s.byte_order == "motorola" and s.signed
    assert s.factor == -0.5 and s.offset == 40
    assert s.receivers == ("B",)


def test_parse_single_layer_mux():
    db = parse_dbc(build(
        bo(300, "M", 8),
        sg("Id", "0|8@1+", "(1,0) [0|255] \"\" A", mux="M"),
        sg("V0", "8|8@1+", mux="m0"),
        sg("V1", "8|8@1+", mux="m1"),
    ))
    m = db.messages[0]
    assert m.mux_switch == "Id"
    kinds = {s.name: (s.mux_kind, s.mux_id) for s in m.signals}
    assert kinds == {"Id": ("M", None), "V0": ("m", 0), "V1": ("m", 1)}


def test_max_standard_frame_id_accepted():
    db = parse_dbc(build(bo(2047, "MAX", 8), sg("S", "0|1@1+")))
    assert db.messages[0].frame_id == 0x7FF


# -- rejections -------------------------------------------------------------- #

def test_extended_frame_id_rejected():
    # Bit 31 set marks an extended ID in DBC.
    with pytest.raises(DBCParseError, match="extended .29-bit"):
        parse_dbc(build(bo(0x80000100, "M", 8), sg("S", "0|1@1+")))


def test_above_standard_id_rejected():
    with pytest.raises(DBCParseError, match="standard 11-bit"):
        parse_dbc(build(bo(2048, "M", 1), sg("S", "0|1@1+")))


def test_bit_width_zero_rejected():
    with pytest.raises(DBCParseError, match="illegal bit width"):
        parse_dbc(build(bo(1, "M", 1), sg("S", "0|0@1+")))


def test_bit_width_65_rejected():
    with pytest.raises(DBCParseError, match="illegal bit width"):
        parse_dbc(build(bo(1, "M", 8), sg("S", "0|65@1+")))


def test_signal_past_dlc_rejected():
    with pytest.raises(DBCParseError, match="does not fit"):
        parse_dbc(build(bo(1, "M", 1), sg("S", "0|16@1+")))


def test_signal_overlap_same_branch_rejected():
    with pytest.raises(DBCParseError, match="overlap"):
        parse_dbc(build(
            bo(1, "M", 2),
            sg("A", "0|12@1+"),
            sg("B", "8|8@1+"),
        ))


def test_signal_overlap_mux_branch_with_plain_rejected():
    with pytest.raises(DBCParseError, match="always-active"):
        parse_dbc(build(
            bo(1, "M", 2),
            sg("Id", "0|4@1+", mux="M"),
            sg("P", "4|8@1+"),                 # plain signal, always active
            sg("V", "8|8@1+", mux="m0"),       # branch overlaps P at bit 8
        ))


def test_overlap_between_different_mux_branches_allowed():
    # Same bytes reused for branch 0 and 1 — legal multiplexing.
    db = parse_dbc(build(
        bo(1, "M", 2),
        sg("Id", "0|8@1+", mux="M"),
        sg("V0", "8|8@1+", mux="m0"),
        sg("V1", "8|8@1+", mux="m1"),
    ))
    assert len(db.messages[0].signals) == 3


def test_multiple_mux_switches_rejected():
    with pytest.raises(DBCParseError, match="multiple"):
        parse_dbc(build(
            bo(1, "M", 2),
            sg("Id1", "0|4@1+", mux="M"),
            sg("Id2", "4|4@1+", mux="M"),
        ))


def test_extended_multiplex_syntax_rejected():
    with pytest.raises(DBCParseError):
        parse_dbc(build(
            bo(1, "M", 2),
            sg("Id", "0|4@1+", mux="M"),
            # extended multiplexing: m0M means branch 0 AND a switch
            sg("X", "8|8@1+", mux="m0M"),
        ))


def test_unsupported_val_table_rejected():
    text = build(bo(1, "M", 1), sg("S", "0|2@1+"))
    text += 'VAL_ 1 S 0 "OFF" 1 "ON" ;\n'
    with pytest.raises(DBCParseError, match="unsupported DBC construct 'VAL_"):
        parse_dbc(text)


def test_unsupported_attribute_rejected():
    text = build(bo(1, "M", 1), sg("S", "0|2@1+"))
    text += 'BA_DEF_ BO_ "GenMsgCycleTime" INT 0 65535;\n'
    with pytest.raises(DBCParseError, match="unsupported DBC construct 'BA_DEF_"):
        parse_dbc(text)


def test_sg_without_bo_rejected():
    with pytest.raises(DBCParseError, match="without a preceding BO_"):
        parse_dbc(sg("S", "0|1@1+"))


def test_duplicate_frame_id_rejected():
    with pytest.raises(DBCParseError, match="duplicate BO_"):
        parse_dbc(build(
            bo(1, "M", 1), sg("S", "0|1@1+"),
            bo(1, "N", 1), sg("T", "0|1@1+"),
        ))


def test_receiver_not_in_nodes_rejected():
    with pytest.raises(DBCParseError, match="not declared in BU_"):
        parse_dbc(build(bo(1, "M", 1), sg("S", "0|1@1+", rest='(1,0) [0|0] "" GHOST')))


def test_dlc_zero_with_signals_rejected():
    with pytest.raises(DBCParseError, match="DLC 0"):
        parse_dbc(build(bo(1, "M", 0), sg("S", "0|1@1+")))
