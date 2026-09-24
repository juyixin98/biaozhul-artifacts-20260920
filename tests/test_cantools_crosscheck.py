"""Cross-validation against the mature ``cantools`` library (v44+).

Strategy: build hundreds of random-but-valid single-signal DBC snippets,
parse them with cantools, generate random frames, and require our decoder
to produce byte-identical raw integers, identical signed interpretation
and identical physical values. Byte order, sign and scaling are all
exercised, including unaligned start bits and signals crossing bytes.

cantools is a *test-only* dependency; the service itself never imports it.
"""

from __future__ import annotations

import io
import random
import struct

import cantools
import pytest

from app.bitops import INTEL, MOTOROLA
from app.dbc import parse_dbc
from app.decoder import decode_frame

pytestmark = pytest.mark.skipif(
    tuple(int(x) for x in cantools.__version__.split(".")[:2]) < (36, 0),
    reason="cantools >= 36 required for the encoder used here",
)


def build_dbc(frame_id, dlc, name, start, length, order, signed,
              factor, offset, mux_line=""):
    at = "1" if order == INTEL else "0"
    sign = "-" if signed else "+"
    mux = f" {mux_line}" if mux_line else ""
    return (
        f'VERSION "x"\nNS_ :\n BS_:\nBU_: A\n'
        f"BO_ {frame_id} M{frame_id}: {dlc} A\n"
        f' SG_ {name}{mux} : {start}|{length}@{at}{sign} '
        f'({factor},{offset}) [0|1] "" A\n'
    )


def encode_with_cantools(db, signal_name, raw, dlc):
    """Encode a frame with one signal set to ``raw`` via cantools."""

    message = db.messages[0]
    encoded = message.encode({signal_name: raw})
    # cantools returns exactly dlc bytes for classic CAN
    assert len(encoded) == dlc, (len(encoded), dlc)
    return list(encoded)


@pytest.mark.parametrize("order", [INTEL, MOTOROLA])
@pytest.mark.parametrize("signed", [False, True])
def test_random_layouts_match_cantools(order, signed, seed=0):
    rng = random.Random(f"{order}-{signed}-{seed}")
    checked = 0
    for trial in range(220):
        dlc = rng.randint(1, 8)
        length = rng.randint(1, min(64, 8 * dlc))
        # Choose a start by asking cantools where such signals are legal:
        # brute-force candidates and keep the first both parsers accept.
        candidates = list(range(8 * dlc))
        rng.shuffle(candidates)
        factor = rng.choice([1, 0.5, 0.25, 0.1, 2, 0.01])
        offset = rng.choice([0, -40, 100, -0.5])
        chosen = None
        for start in candidates:
            snippet = build_dbc(
                100 + trial, dlc, "S", start, length, order, signed,
                factor, offset,
            )
            try:
                our_db = parse_dbc(snippet)
                ct_db = cantools.database.load_string(
                    snippet, database_format="dbc"
                )
            except Exception:  # noqa: BLE001 - invalid combo, skip candidate
                continue
            # cantools sometimes accepts layouts our sawtooth reader
            # rejects or vice-versa for Motorola; require BOTH to accept.
            chosen = (start, snippet, our_db, ct_db)
            break
        if chosen is None:
            continue
        start, snippet, our_db, ct_db = chosen

        message = ct_db.messages[0]
        ct_sig = message.signals[0]
        lo = ct_sig.minimum if ct_sig.minimum is not None else 0
        hi = ct_sig.maximum if ct_sig.maximum is not None else 1
        # We encode via RAW integer rather than physical to avoid cantools'
        # range choices; pick raw codes covering boundaries.
        codes = [0, 1, (1 << length) - 1]
        if signed:
            codes += [1 << (length - 1), (1 << (length - 1)) + 1]
        codes = sorted(set(c for c in codes if c < (1 << min(length, 63))))

        for raw in codes:
            try:
                # scaling=False -> ``raw`` is the wire integer code, not the
                # physical value; strict=False allows boundary raw codes.
                data = message.encode({"S": raw}, scaling=False,
                                      strict=False)
            except Exception:  # noqa: BLE001
                continue
            data = list(data)
            if len(data) != dlc:
                continue
            # cantools reference values
            ct_decoded = message.decode(bytes(data), decode_choices=False,
                                        scaling=True)
            ct_physical = ct_decoded["S"]
            ct_raw = message.decode(bytes(data), decode_choices=False,
                                    scaling=False)["S"]

            our_frame = decode_frame(
                our_db.messages[0], data, "v"
            )
            our_sig = our_frame.signals[0]
            assert our_sig.raw == raw, (
                f"raw mismatch order={order} signed={signed} "
                f"{start}|{length} wire={data} ours={our_sig.raw} want={raw}\n"
                f"{snippet}"
            )
            # Our raw is always the UNSIGNED wire pattern; compare signed
            # through the physical value instead.
            assert our_sig.raw == (
                ct_raw if ct_raw >= 0 else ct_raw + (1 << length)
            )
            assert abs(our_sig.value - float(ct_physical)) < max(
                1e-6, abs(float(ct_physical)) * 1e-9
            ), (f"physical mismatch {start}|{length} {order} signed={signed} "
                f"raw={raw} ours={our_sig.value} cantools={ct_physical}")
            checked += 1
    assert checked > 300, f"only {checked} cross-checks ran"


def test_cantools_parses_demo_dbc_and_agrees_on_examples():
    """The shipped demo.dbc must be a valid DBC for cantools, and every
    example payload must decode identically."""

    text = open("examples/demo.dbc", encoding="utf-8").read()
    ct_db = cantools.database.load_string(text, database_format="dbc")
    our_db = parse_dbc(text)

    cases = [
        (256, [0x34, 0x12, 0xD8, 0x64, 0, 0, 0, 0]),
        (300, [0x01, 0, 0x40, 0x06, 0, 0, 0, 0]),
        (300, [0x02, 0, 0, 0, 0x06, 0x46, 0xE0, 0]),
        (2047, [0x12, 0x34, 0xFC, 0x18, 0x60, 0x09, 0, 0]),
    ]
    for frame_id, data in cases:
        ct_msg = ct_db.get_message_by_frame_id(frame_id)
        our_msg = our_db.message_by_id(frame_id)
        ct_raw = ct_msg.decode(bytes(data), decode_choices=False,
                               scaling=False)
        our_frame = decode_frame(our_msg, data, "v")
        our_names = {s.name for s in our_frame.signals}
        for name in our_names:
            assert name in ct_raw, f"{name} missing in cantools decode"
            ct_val = ct_raw[name]
            ours = next(s for s in our_frame.signals if s.name == name)
            # raw comparison accounting for two's-complement representation
            expected_raw = (
                ct_val if ct_val >= 0 else ct_val + (1 << ours.length)
            ) if ours.signed else ct_val
            assert ours.raw == expected_raw, (
                f"{frame_id}.{name}: raw ours={ours.raw} ct={ct_val}"
            )


def test_cantools_multiplex_branch_selection_matches():
    text = open("examples/demo.dbc", encoding="utf-8").read()
    ct_db = cantools.database.load_string(text, database_format="dbc")
    our_db = parse_dbc(text)
    ct_msg = ct_db.get_message_by_frame_id(300)
    our_msg = our_db.message_by_id(300)

    # Only mux values 1 and 2 are defined; cantools raises on an undefined
    # selector while this service returns the switch signal alone. Verify
    # the two implementations agree on the *defined* branches.
    for mux in (1, 2):
        data = {
            1: [0x01, 0, 0x40, 0x06, 0, 0, 0, 0],
            2: [0x02, 0, 0, 0, 0x06, 0x46, 0xE0, 0],
        }[mux]
        decoded = ct_msg.decode(bytes(data), decode_choices=False,
                                scaling=False, allow_truncated=False)
        active = {k for k, v in decoded.items() if v is not None}
        our_frame = decode_frame(our_msg, data, "v")
        assert {s.name for s in our_frame.signals} == active


def test_undefined_mux_value_returns_switch_only():
    # Deliberate (documented) behaviour: an unknown selector decodes only
    # the always-present multiplexor switch; cantools instead raises.
    our_db = parse_dbc(open("examples/demo.dbc", encoding="utf-8").read())
    our_msg = our_db.message_by_id(300)
    frame = decode_frame(our_msg, [0x09, 0, 0, 0, 0, 0, 0, 0], "v")
    assert [s.name for s in frame.signals] == ["BrakeMux"]
