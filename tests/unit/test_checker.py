"""Offline unit tests for the storage-layout compatibility checker.

These run without Anvil. They use (a) the real exported compiler bundles in
layouts/ for the headline scenarios and (b) hand-built minimal layouts for
edge cases that the contract suite does not exercise.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from checker import check_layouts, is_gap_label, parse_layout

ROOT = Path(__file__).resolve().parents[2]
LAYOUTS = ROOT / "layouts"


def bundle(name):
    return json.loads((LAYOUTS / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# Real compiler bundles: the acceptance scenarios
# ---------------------------------------------------------------------------


def test_append_fields_is_compatible():
    r = check_layouts(bundle("BoxV1"), bundle("BoxV2"))
    assert r.compatible is True
    added = {a["label"] for a in r.added}
    assert {"extra", "a", "b"} <= added


def test_reorder_is_blocked():
    r = check_layouts(bundle("BoxV1"), bundle("BoxV3"))
    assert r.compatible is False
    codes = {f.code for f in r.errors}
    assert "SLOT_MOVED" in codes
    # name moved from 63 to 61
    name_err = next(f for f in r.errors if f.label == "name" and f.code == "SLOT_MOVED")
    assert name_err.old_slot == 63 and name_err.new_slot == 61


def test_type_widening_and_dynamic_change_blocked():
    r = check_layouts(bundle("BoxV1"), bundle("BoxV4"))
    assert r.compatible is False
    codes = {f.code for f in r.errors}
    # y: uint32 -> uint256
    assert "TYPE_CHANGED" in codes
    # counts: uint256[] -> mapping
    dyn = [f for f in r.errors if f.label == "counts" and f.code == "DYNAMIC_TYPE_CHANGED"]
    assert dyn, "dynamic array -> mapping must be flagged"


def test_base_class_widening_blocked():
    r = check_layouts(bundle("BoxV1"), bundle("BoxV5"))
    assert r.compatible is False
    labels = {f.label for f in r.errors}
    # baseCounter widening inside the base contract is caught structurally
    assert "baseCounter" in labels
    assert "baseAdmin" in labels  # pushed to a later slot


def test_safe_base_gap_consumption_compatible():
    r = check_layouts(bundle("BoxV1"), bundle("BoxV6"))
    assert r.compatible is True, [f.message for f in r.errors]
    # baseName is new, consuming the head of the old __baseGap
    assert any(a["label"] == "baseName" for a in r.added)


def test_inheritance_change_is_warning_not_blocker():
    # v2 extends v1 (leaf name differs); inheritance warning should not block
    r = check_layouts(bundle("BoxV1"), bundle("BoxV2"))
    assert all(f.severity != "error" for f in r.warnings)
    assert any(f.code == "INHERITANCE_CHANGED" for f in r.warnings)


# ---------------------------------------------------------------------------
# Synthetic minimal layouts for edge cases
# ---------------------------------------------------------------------------


def _doc(variables):
    """Build a solc-shaped storageLayout from (label, type, slot, offset)."""
    type_reg = {
        "t_uint256": {"encoding": "inplace", "label": "uint256", "numberOfBytes": "32"},
        "t_uint64": {"encoding": "inplace", "label": "uint64", "numberOfBytes": "8"},
        "t_uint128": {"encoding": "inplace", "label": "uint128", "numberOfBytes": "16"},
        "t_address": {"encoding": "inplace", "label": "address", "numberOfBytes": "20"},
        "t_string": {"encoding": "bytes", "label": "string", "numberOfBytes": "32"},
        "t_bytes": {"encoding": "bytes", "label": "bytes", "numberOfBytes": "32"},
        "t_arr_dyn": {
            "encoding": "dynamic_array",
            "label": "uint256[]",
            "numberOfBytes": "32",
            "base": "t_uint256",
        },
        "t_mapping": {
            "encoding": "mapping",
            "label": "mapping(uint256 => uint256)",
            "numberOfBytes": "32",
            "key": "t_uint256",
            "value": "t_uint256",
        },
        "t_gap2": {
            "encoding": "inplace",
            "label": "uint256[2]",
            "numberOfBytes": "64",
        },
        "t_gap1": {
            "encoding": "inplace",
            "label": "uint256[1]",
            "numberOfBytes": "32",
        },
    }
    storage = []
    for i, (label, tkey, slot, off) in enumerate(variables):
        storage.append(
            {"label": label, "type": tkey, "slot": str(slot), "offset": off, "contract": "C"}
        )
    return {"storageLayout": {"storage": storage, "types": type_reg}, "contract": "C"}


def V0():
    return _doc(
        [
            ("a", "t_uint256", 0, 0),
            ("__gap", "t_gap2", 1, 0),
            ("b", "t_uint64", 3, 0),
        ]
    )


def test_identical_layout_is_compatible():
    r = check_layouts(V0(), V0())
    assert r.compatible is True


def test_append_after_tail_ok():
    new = _doc(
        [
            ("a", "t_uint256", 0, 0),
            ("__gap", "t_gap2", 1, 0),
            ("b", "t_uint64", 3, 0),
            ("c", "t_uint256", 4, 0),
        ]
    )
    assert check_layouts(V0(), new).compatible is True


def test_new_field_over_existing_is_collision():
    new = _doc(
        [
            ("a", "t_uint256", 0, 0),
            ("__gap", "t_gap2", 1, 0),
            ("b", "t_uint64", 3, 0),
            ("c", "t_uint256", 0, 0),  # collides with a
        ]
    )
    r = check_layouts(V0(), new)
    assert r.compatible is False
    assert any(f.code == "SLOT_COLLISION" and f.label == "c" for f in r.errors)


def test_field_removal_blocked():
    new = _doc(
        [
            ("a", "t_uint256", 0, 0),
            ("__gap", "t_gap2", 1, 0),
        ]
    )
    r = check_layouts(V0(), new)
    assert r.compatible is False
    assert any(f.code == "VAR_REMOVED" and f.label == "b" for f in r.errors)


def test_widening_packed_field_changes_offset():
    old = _doc([("x", "t_uint64", 0, 0), ("y", "t_uint64", 0, 8)])
    new = _doc([("x", "t_uint256", 0, 0), ("y", "t_uint64", 1, 0)])
    r = check_layouts(old, new)
    assert r.compatible is False
    codes = {f.code for f in r.errors}
    assert "TYPE_CHANGED" in codes and "SLOT_MOVED" in codes


def test_dynamic_array_to_mapping_blocked():
    old = _doc([("items", "t_arr_dyn", 0, 0)])
    new = _doc([("items", "t_mapping", 0, 0)])
    r = check_layouts(old, new)
    assert r.compatible is False
    assert any(f.code == "DYNAMIC_TYPE_CHANGED" for f in r.errors)


def test_string_to_bytes_encoding_equiv_but_label_change():
    # string and bytes share the "bytes" encoding; solc distinguishes them by
    # label — the checker must still treat the label/type swap as a change
    # because semantics differ.
    old = _doc([("s", "t_string", 0, 0)])
    new = _doc([("s", "t_bytes", 0, 0)])
    r = check_layouts(old, new)
    # labels differ (string vs bytes) => TYPE_CHANGED even though encoding same
    assert r.compatible is False


def test_gap_consume_head_with_new_field_ok():
    new = _doc(
        [
            ("a", "t_uint256", 0, 0),
            ("c", "t_uint256", 1, 0),  # consumes first gap slot
            ("__gap", "t_gap1", 2, 0),  # gap shrinks, head moves to 2
            ("b", "t_uint64", 3, 0),
        ]
    )
    r = check_layouts(V0(), new)
    assert r.compatible is True, [f.message for f in r.errors]
    assert any(f.code == "GAP_CONSUMED" for f in r.infos)


def test_gap_head_move_without_new_field_blocked():
    new = _doc(
        [
            ("a", "t_uint256", 0, 0),
            ("__gap", "t_gap1", 2, 0),  # head moved but slot 1 empty
            ("b", "t_uint64", 3, 0),
        ]
    )
    r = check_layouts(V0(), new)
    assert r.compatible is False
    assert any(f.code == "GAP_MOVED" for f in r.errors)


def test_gap_growth_blocked():
    # gap grows backwards over existing data is impossible here, but growing
    # forward over a previously-used tail must fail
    old = _doc([("a", "t_uint256", 0, 0), ("__gap", "t_gap1", 1, 0), ("b", "t_uint64", 2, 0)])
    new = _doc([("a", "t_uint256", 0, 0), ("__gap", "t_gap2", 1, 0), ("b", "t_uint64", 3, 0)])
    r = check_layouts(old, new)
    # b moved AND gap grew — at least one blocker must mention the gap
    assert r.compatible is False
    assert any(f.code in {"GAP_GREW", "GAP_MOVED"} for f in r.errors)


def test_offset_change_within_same_slot_blocked():
    old = _doc([("x", "t_uint64", 0, 0), ("y", "t_uint64", 0, 8)])
    new = _doc([("x", "t_uint64", 0, 8), ("y", "t_uint64", 0, 0)])  # swapped offsets
    r = check_layouts(old, new)
    assert r.compatible is False
    assert any(f.code == "OFFSET_MOVED" for f in r.errors)


def test_malformed_document_raises():
    with pytest.raises(ValueError):
        parse_layout(["not", "an", "object"])
    with pytest.raises(ValueError):
        parse_layout({"unexpected": 1})


def test_gap_label_recogniser():
    assert is_gap_label("__gap")
    assert is_gap_label("__baseGap")
    assert is_gap_label("gap0")
    assert not is_gap_label("gapStore")
    assert not is_gap_label("_gap_value")
