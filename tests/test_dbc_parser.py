"""DBC parser tests: accepted subset and every explicit rejection path."""

from __future__ import annotations

import pytest

from app.bitops import INTEL, MOTOROLA
from app.dbc import (
    DBCLayoutError,
    DBCSyntaxError,
    parse_dbc,
)

HEADER = '''
VERSION "t"

NS_ :
    CM_
    BA_DEF_
    BA_

BS_:

BU_: A B
'''


def bo(frame_id, dlc, signals_text, *, name=None):
    name = name or f"MSG{frame_id}"
    return (
        f"BO_ {frame_id} {name}: {dlc} A\n"
        + "\n".join(f" SG_ {line}" for line in signals_text)
    )


def parse(messages_text: str):
    return parse_dbc(HEADER + messages_text)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_parse_basic_fields():
    db = parse(bo(100, 8, [
        'S1 : 0|8@1+ (0.5,10) [0|137.5] "km/h" B',
        'S2 : 23|16@0- (2,-1) [0|10] "V" B',
    ]))
    msg = db.message_by_id(100)
    assert msg is not None and msg.dlc == 8 and msg.transmitter == "A"
    s1, s2 = msg.signals
    assert s1.byte_order is INTEL or s1.byte_order == "intel"
    assert not s1.signed
    assert float(s1.factor) == 0.5 and float(s1.offset) == 10
    assert s2.byte_order == MOTOROLA
    assert s2.signed
    assert db.nodes == ("A", "B")


def test_parse_multiline_comment_is_skipped():
    db = parse_dbc(HEADER + bo(1, 1, ['S : 0|8@1+ (1,0) [0|255] "" B']) +
                   '\nCM_ BO_ 1 "line one\nline two";\n')
    assert db.message_by_id(1).name == "MSG1"


def test_parse_multiplex_single_level():
    db = parse(bo(200, 4, [
        'Sel M0 : 0|4@1+ (1,0) [0|15] "" B',
        'A m1 : 8|8@1+ (1,0) [0|1] "" B',
        'B m2 : 8|8@1+ (1,0) [0|1] "" B',
    ]))
    msg = db.message_by_id(200)
    kinds = {s.name: s.mux_kind for s in msg.signals}
    assert kinds == {"Sel": "switch", "A": "branch", "B": "branch"}
    assert msg.mux_switch.name == "Sel"


def test_overlap_allowed_between_different_branches():
    db = parse(bo(201, 4, [
        'Sel M0 : 0|4@1+ (1,0) [0|15] "" B',
        'A m1 : 8|8@1+ (1,0) [0|255] "" B',
        'B m2 : 8|8@1+ (1,0) [0|255] "" B',  # same bits, other branch
    ]))
    assert {s.name for s in db.message_by_id(201).signals} == {
        "Sel", "A", "B"
    }


# --------------------------------------------------------------------------- #
# Explicit rejections
# --------------------------------------------------------------------------- #
def test_empty_input_rejected():
    with pytest.raises(DBCSyntaxError, match="empty"):
        parse_dbc("   \n")


def test_no_messages_rejected():
    with pytest.raises(DBCSyntaxError, match="no BO_ message"):
        parse_dbc(HEADER)


def test_unknown_keyword_rejected():
    text = HEADER + 'BA_DEF_ BO_ "GenMsgCycleTime" INT 0 65535;\n' + bo(
        1, 1, ['S : 0|8@1+ (1,0) [0|255] "" B']
    )
    with pytest.raises(DBCSyntaxError, match="unsupported DBC statement"):
        parse_dbc(text)


def test_val_table_rejected():
    text = HEADER + 'VAL_TABLE_ yesno 0 "No" 1 "Yes" ;\n' + bo(
        1, 1, ['S : 0|8@1+ (1,0) [0|255] "" B']
    )
    with pytest.raises(DBCSyntaxError, match="unsupported DBC statement"):
        parse_dbc(text)


def test_float_signal_type_rejected():
    # SIG_VALTYPE_ ... 1 marks a float signal - explicitly unsupported.
    text = HEADER + bo(1, 4, ['S : 0|32@1+ (1,0) [0|1] "" B']) + \
        "\nSIG_VALTYPE_ 1 S 1;\n"
    with pytest.raises(DBCSyntaxError, match="unsupported DBC statement"):
        parse_dbc(text)


def test_extended_frame_id_rejected():
    with pytest.raises(DBCSyntaxError, match="extended CAN frame id"):
        parse(bo(0x80000000 + 256, 8, [
            'S : 0|8@1+ (1,0) [0|255] "" B',
        ]))


def test_frame_id_above_standard_max_rejected():
    # 0x800 without the extended flag is still not an 11-bit id
    with pytest.raises(DBCSyntaxError, match="11-bit maximum"):
        parse(bo(0x800, 8, ['S : 0|8@1+ (1,0) [0|255] "" B']))


def test_max_standard_frame_id_accepted():
    db = parse(bo(0x7FF, 8, ['S : 0|8@1+ (1,0) [0|255] "" B']))
    assert db.message_by_id(0x7FF) is not None


def test_dlc_above_8_rejected():
    with pytest.raises(DBCSyntaxError, match="invalid DLC"):
        parse(bo(1, 9, ['S : 0|8@1+ (1,0) [0|255] "" B']))


def test_bit_width_zero_rejected():
    with pytest.raises(DBCLayoutError, match="illegal bit width"):
        parse(bo(1, 8, ['S : 0|0@1+ (1,0) [0|0] "" B']))


def test_bit_width_65_rejected():
    with pytest.raises(DBCLayoutError, match="64 bits"):
        parse(bo(1, 8, ['S : 0|65@1+ (1,0) [0|0] "" B']))


def test_signal_overflowing_dlc_rejected():
    # 8|16@1 at DLC 2: width fits 16 bits but positions 16..23 spill into
    # a non-existent byte 3 -> frame-boundary error
    with pytest.raises(DBCLayoutError, match="outside the 2-byte frame"):
        parse(bo(1, 2, ['S : 8|16@1+ (1,0) [0|0] "" B']))


def test_motorola_signal_overflowing_dlc_rejected():
    with pytest.raises(DBCLayoutError, match="outside the 2-byte frame"):
        parse(bo(1, 2, ['S : 11|12@0+ (1,0) [0|0] "" B']))


def test_same_branch_overlap_rejected():
    with pytest.raises(DBCLayoutError, match="overlap"):
        parse(bo(1, 4, [
            'Sel M0 : 0|4@1+ (1,0) [0|15] "" B',
            'A m1 : 8|8@1+ (1,0) [0|255] "" B',
            'C m1 : 12|4@1+ (1,0) [0|15] "" B',  # overlaps A
        ]))


def test_plain_signal_overlapping_branch_rejected():
    with pytest.raises(DBCLayoutError, match="overlap"):
        parse(bo(1, 4, [
            'Sel M0 : 0|4@1+ (1,0) [0|15] "" B',
            'A m1 : 8|8@1+ (1,0) [0|255] "" B',
            'P : 8|8@1+ (1,0) [0|255] "" B',  # plain: always present
        ]))


def test_signal_overlapping_switch_rejected():
    with pytest.raises(DBCLayoutError, match="overlap"):
        parse(bo(1, 1, [
            'Sel M0 : 0|8@1+ (1,0) [0|255] "" B',
            'P : 0|8@1+ (1,0) [0|255] "" B',
        ]))


def test_extended_multiplex_marker_rejected():
    with pytest.raises(DBCSyntaxError, match="extended multiplexing"):
        parse(bo(1, 4, [
            'Sel M0 : 0|4@1+ (1,0) [0|15] "" B',
            'A m1m0 : 8|8@1+ (1,0) [0|1] "" B',
        ]))


def test_branch_without_switch_rejected():
    with pytest.raises(DBCLayoutError, match="no multiplexor switch"):
        parse(bo(1, 4, ['A m1 : 8|8@1+ (1,0) [0|1] "" B']))


def test_two_switches_rejected():
    with pytest.raises(DBCLayoutError, match="exactly one"):
        parse(bo(1, 4, [
            'S1 M0 : 0|4@1+ (1,0) [0|15] "" B',
            'S2 M0 : 8|4@1+ (1,0) [0|15] "" B',
        ]))


def test_branch_value_exceeding_switch_width_rejected():
    with pytest.raises(DBCLayoutError, match="cannot be selected"):
        parse(bo(1, 4, [
            'Sel M0 : 0|2@1+ (1,0) [0|3] "" B',  # max selector = 3
            'A m4 : 8|8@1+ (1,0) [0|1] "" B',
        ]))


def test_duplicate_signal_name_rejected():
    with pytest.raises(DBCSyntaxError, match="duplicate signal"):
        parse(bo(1, 2, [
            'S : 0|8@1+ (1,0) [0|1] "" B',
            'S : 8|8@1+ (1,0) [0|1] "" B',
        ]))


def test_duplicate_frame_id_rejected():
    text = HEADER + bo(1, 1, ['S : 0|8@1+ (1,0) [0|1] "" B']) + "\n" + \
        bo(1, 1, ['T : 0|8@1+ (1,0) [0|1] "" B'], name="OTHER")
    with pytest.raises(DBCSyntaxError, match="duplicate BO_"):
        parse_dbc(text)


def test_sg_without_bo_rejected():
    with pytest.raises(DBCSyntaxError, match="outside of any BO_"):
        parse_dbc(HEADER + ' SG_ S : 0|8@1+ (1,0) [0|1] "" B\n')


def test_malformed_signal_line_rejected():
    with pytest.raises(DBCSyntaxError, match="malformed SG_"):
        parse(bo(1, 1, ['S 0 8 @1 (1,0)']))


def test_bad_factor_rejected():
    with pytest.raises(DBCSyntaxError, match="invalid factor"):
        parse(bo(1, 1, ['S : 0|8@1+ (abc,0) [0|1] "" B']))


def test_unterminated_comment_rejected():
    with pytest.raises(DBCSyntaxError, match="unterminated CM_"):
        parse_dbc(HEADER + bo(1, 1, ['S : 0|8@1+ (1,0) [0|1] "" B']) +
                  '\nCM_ BO_ 1 "never closed\n')
