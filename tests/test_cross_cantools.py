"""Cross-validation against the mature ``cantools`` library (44.x).

Two layers:

1. Exhaustive single-signal sweep over every start bit (0..63), length
   (1..64), both byte orders and signedness: wherever cantools accepts a
   layout, our parser must accept it and decode random frames identically;
   wherever cantools rejects it, we must reject it too.

2. Multi-signal / multiplexed fixtures: hand-built messages with overlaps
   across mux branches and the maximum standard frame id, compared on
   random valid frames.
"""

from __future__ import annotations

import random

import cantools
import pytest

from app.dbc import parse_dbc
from app.decoder import decode_frame


def cantools_dbc(start, length, order, sign):
    return (
        'VERSION ""\n\nNS_:\n\tNS_DESC_\n\nBS_:\n\nBU_: ECU\n\n'
        f"BO_ 100 M: 8 ECU\n"
        f' SG_ S : {start}|{length}@{order}{sign} (1,0) [0|0] "" ECU\n'
    )


@pytest.mark.parametrize("order", [0, 1], ids=["motorola", "intel"])
@pytest.mark.parametrize("sign", ["+", "-"], ids=["unsigned", "signed"])
def test_exhaustive_layouts_vs_cantools(order, sign):
    rng = random.Random(f"{order}-{sign}")
    accepted = rejected = compared = 0
    for start in range(64):
        for length in range(1, 65):
            text = cantools_dbc(start, length, order, sign)

            # cantools acceptance
            try:
                cdb = cantools.database.load_string(text, database_format="dbc")
            except Exception:
                cdb = None

            # our acceptance
            try:
                ours = parse_dbc(text)
            except Exception:
                ours = None

            if cdb is None:
                rejected += 1
                assert ours is None, (
                    f"we accepted a layout cantools rejects: "
                    f"start={start} len={length} order={order} sign={sign}"
                )
                continue
            assert ours is not None, (
                f"we rejected a layout cantools accepts: "
                f"start={start} len={length} order={order} sign={sign}"
            )
            accepted += 1

            cmsg = cdb.get_message_by_frame_id(100)
            for _ in range(3):
                data = bytes(rng.randrange(256) for _ in range(8))
                craw = cmsg.decode(
                    data, decode_choices=False, scaling=False
                )["S"]
                frame = decode_frame(ours, 100, data)
                oraw = frame.signals[0].raw
                assert oraw == craw, (
                    f"raw mismatch start={start} len={length} "
                    f"order={order} sign={sign} data={data.hex()} "
                    f"ours={oraw} cantools={craw}"
                )
                compared += 1

    # Sanity: the sweep genuinely exercised both outcomes and many frames.
    # In an 8-byte message exactly 2080 of the 4096 start/length layouts fit.
    assert accepted == 2080 and rejected == 2016 and compared == 6240


# --------------------------------------------------------------------------- #
# Scaled / offset cross-check on random values
# --------------------------------------------------------------------------- #

SCALED_DBC = (
    'VERSION ""\n\nNS_:\n\tNS_DESC_\n\nBS_:\n\nBU_: A B\n\n'
    "BO_ 256 M: 8 A\n"
    ' SG_ RPM : 0|16@1+ (0.25,100) [0|0] "rpm" B\n'
    ' SG_ TEMP : 16|8@1- (1,-40) [0|0] "degC" B\n'
    ' SG_ SPD : 24|16@0+ (0.01,0) [0|0] "km/h" B\n'
    ' SG_ NEG : 40|12@0- (0.5,-3.5) [0|0] "" B\n'
)


def test_scaled_values_vs_cantools():
    cdb = cantools.database.load_string(SCALED_DBC, database_format="dbc")
    ours = parse_dbc(SCALED_DBC)
    rng = random.Random(42)
    cmsg = cdb.get_message_by_frame_id(256)
    for _ in range(300):
        data = bytes(rng.randrange(256) for _ in range(8))
        c = cmsg.decode(data, decode_choices=False, scaling=True)
        o = decode_frame(ours, 256, data)
        for s in o.signals:
            assert s.physical == pytest.approx(c[s.name], abs=1e-9), (
                f"{s.name}: ours={s.physical} cantools={c[s.name]}"
            )


# --------------------------------------------------------------------------- #
# Multiplexed message cross-check
# --------------------------------------------------------------------------- #

MUX_DBC = (
    'VERSION ""\n\nNS_:\n\tNS_DESC_\n\nBS_:\n\nBU_: A B\n\n'
    "BO_ 1024 X: 8 A\n"
    ' SG_ ID M : 0|8@1+ (1,0) [0|0] "" B\n'
    ' SG_ V0 m0 : 8|16@1+ (0.001,0) [0|0] "V" B\n'
    ' SG_ P1 m1 : 8|16@1+ (0.1,0) [0|0] "kPa" B\n'
    ' SG_ B2 m2 : 24|8@0- (2,1) [0|0] "" B\n'
)


def test_multiplexed_message_vs_cantools():
    cdb = cantools.database.load_string(MUX_DBC, database_format="dbc")
    ours = parse_dbc(MUX_DBC)
    cmsg = cdb.get_message_by_frame_id(1024)
    rng = random.Random(7)
    for switch in range(3):
        for _ in range(20):
            payload = bytearray(rng.randrange(256) for _ in range(8))
            payload[0] = switch
            data = bytes(payload)
            c = cmsg.decode(data, decode_choices=False, scaling=True)
            o = decode_frame(ours, 1024, data)
            assert o.mux_value == switch
            for s in o.signals:
                if not s.present:
                    assert s.name not in c or c.get(s.name) is None
                    continue
                assert s.raw == cmsg.decode(
                    data, decode_choices=False, scaling=False
                )[s.name]
                assert s.physical == pytest.approx(c[s.name], abs=1e-9)


def test_max_frame_id_fixture_vs_cantools():
    text = (
        'VERSION ""\n\nNS_:\n\tNS_DESC_\n\nBS_:\n\nBU_: A\n\n'
        "BO_ 2047 Z: 8 A\n"
        ' SG_ BIG : 7|32@0- (1,0) [0|0] "" A\n'
        ' SG_ LO : 32|8@1+ (1,0) [0|0] "" A\n'
    )
    cdb = cantools.database.load_string(text, database_format="dbc")
    ours = parse_dbc(text)
    rng = random.Random(99)
    for _ in range(100):
        data = bytes(rng.randrange(256) for _ in range(8))
        c = cdb.get_message_by_frame_id(2047).decode(
            data, decode_choices=False, scaling=False
        )
        o = decode_frame(ours, 2047, data)
        assert o.signals[0].raw == c["BIG"]
        assert o.signals[1].raw == c["LO"]
