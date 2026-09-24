"""Unit tests for the layout checker.

Cases are built two ways:
1. inline layout JSON (fast, exercises each rule in isolation), and
2. real compiler layouts produced by `forge build` for the sample contracts
   (field append, reorder, type widening, base-class change, dynamic-type
   change) — the acceptance scenarios.
"""

from __future__ import annotations

import pytest

from backend.artifacts import get_base_contracts, get_layout
from backend.layout_checker import check_layouts

# --------------------------------------------------------------------------
# Inline layout helpers
# --------------------------------------------------------------------------

UINT256 = {"encoding": "inplace", "label": "uint256", "numberOfBytes": "32"}
UINT128 = {"encoding": "inplace", "label": "uint128", "numberOfBytes": "16"}
ADDRESS = {"encoding": "inplace", "label": "address", "numberOfBytes": "20"}
STRING = {"encoding": "bytes", "label": "string", "numberOfBytes": "32"}
BYTES_DYN = {"encoding": "bytes", "label": "bytes", "numberOfBytes": "32"}
MAPPING = {
    "encoding": "mapping",
    "label": "mapping(address => uint256)",
    "numberOfBytes": "32",
    "key": "t_address",
    "value": "t_uint256",
}
DYN_ARRAY = {
    "encoding": "dynamic_array",
    "label": "uint256[]",
    "numberOfBytes": "32",
    "base": "t_uint256",
}

BASE_TYPES = {
    "t_uint256": UINT256,
    "t_uint128": UINT128,
    "t_address": ADDRESS,
    "t_string": STRING,
    "t_bytes": BYTES_DYN,
    "t_mapping": MAPPING,
    "t_array_dyn": DYN_ARRAY,
}


def var(label, slot, offset=0, type_="t_uint256", contract="C"):
    return {
        "astId": 1,
        "contract": f"C.sol:{contract}",
        "label": label,
        "offset": offset,
        "slot": str(slot),
        "type": type_,
    }


def layout(storage, extra_types=None):
    types = dict(BASE_TYPES)
    types.update(extra_types or {})
    return {"storage": storage, "types": types}


def codes(report):
    return {i.code for i in report.issues}


# --------------------------------------------------------------------------
# Inline cases
# --------------------------------------------------------------------------

def test_append_is_compatible():
    old = layout([var("a", 0), var("b", 1)])
    new = layout([var("a", 0), var("b", 1), var("c", 2)])
    report = check_layouts(old, new, "C", "C")
    assert report.compatible
    assert "VARIABLE_APPENDED" in codes(report)


def test_append_packed_into_last_slot_is_compatible():
    old = layout([var("a", 0, 0, "t_uint128")])
    new = layout([var("a", 0, 0, "t_uint128"), var("b", 0, 16, "t_uint128")])
    report = check_layouts(old, new, "C", "C")
    assert report.compatible


def test_reorder_is_rejected():
    old = layout([var("a", 0), var("b", 1)])
    new = layout([var("b", 0), var("a", 1)])
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "SLOT_MOVED" in codes(report)


def test_removal_is_rejected():
    old = layout([var("a", 0), var("b", 1)])
    new = layout([var("a", 0)])
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "VARIABLE_REMOVED" in codes(report)


def test_type_widening_is_rejected():
    old = layout([var("a", 0, 0, "t_uint128"), var("b", 0, 16, "t_uint128")])
    new = layout([var("a", 0, 0, "t_uint256"), var("b", 1, 0, "t_uint128")])
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "TYPE_CHANGED" in codes(report)
    assert "SLOT_MOVED" in codes(report)  # b got pushed out of slot 0


def test_offset_shift_is_rejected():
    old = layout([var("a", 0, 0, "t_uint128"), var("b", 0, 16, "t_uint128")])
    new = layout([var("a", 0, 16, "t_uint128"), var("b", 0, 0, "t_uint128")])
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "OFFSET_MOVED" in codes(report)


def test_insertion_is_rejected():
    old = layout([var("a", 0), var("b", 1)])
    new = layout([var("inserted", 0), var("a", 1), var("b", 2)])
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "VARIABLE_INSERTED" in codes(report)


def test_dynamic_type_change_is_rejected():
    old = layout([var("items", 0, 0, "t_array_dyn")])
    new = layout([var("items", 0, 0, "t_mapping")])
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "TYPE_CHANGED" in codes(report)


def test_string_to_bytes_is_rejected():
    old = layout([var("data", 0, 0, "t_string")])
    new = layout([var("data", 0, 0, "t_bytes")])
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "TYPE_CHANGED" in codes(report)


def test_struct_member_change_is_rejected():
    old_struct = {
        "encoding": "inplace",
        "label": "struct C.Config",
        "numberOfBytes": "64",
        "members": [
            var("x", 0, 0, "t_uint256"),
            var("y", 1, 0, "t_uint256"),
        ],
    }
    new_struct = {
        "encoding": "inplace",
        "label": "struct C.Config",
        "numberOfBytes": "64",
        "members": [
            var("x", 0, 0, "t_uint256"),
            var("y", 1, 0, "t_uint128"),  # narrowed member
        ],
    }
    old = layout([var("cfg", 0, 0, "t_struct")], {"t_struct": old_struct})
    new = layout([var("cfg", 0, 0, "t_struct")], {"t_struct": new_struct})
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "TYPE_CHANGED" in codes(report)


def test_mapping_value_change_is_rejected():
    new_mapping = dict(MAPPING, value="t_uint128")
    old = layout([var("balances", 0, 0, "t_mapping")])
    new = layout([var("balances", 0, 0, "t_mapping2")], {"t_mapping2": new_mapping})
    report = check_layouts(old, new, "C", "C")
    assert not report.compatible
    assert "TYPE_CHANGED" in codes(report)


def test_inheritance_change_warns():
    old = layout([var("x", 0, contract="BaseA"), var("y", 1, contract="C")])
    new = layout(
        [var("z", 0, contract="BaseB"), var("x", 1, contract="BaseA"), var("y", 2, contract="C")]
    )
    report = check_layouts(
        old, new, "C", "C", old_bases=["BaseA"], new_bases=["BaseB", "BaseA"]
    )
    assert not report.compatible  # inserting a base shifts slots
    assert "INHERITANCE_CHANGED" in codes(report)
    assert "VARIABLE_INSERTED" in codes(report)


# --------------------------------------------------------------------------
# Real compiler layouts (acceptance scenarios)
# --------------------------------------------------------------------------

def test_compiled_append_box_v1_to_v2(compiled_artifacts):
    report = check_layouts(get_layout("BoxV1"), get_layout("BoxV2"), "BoxV1", "BoxV2")
    assert report.compatible, [i.message for i in report.errors]
    appended = [i for i in report.issues if i.code == "VARIABLE_APPENDED"]
    assert any("extra" in i.message for i in appended)
    assert any("balances" in i.message for i in appended)


def test_compiled_reorder_rejected(compiled_artifacts):
    report = check_layouts(
        get_layout("BoxV1"), get_layout("BoxBadReorder"), "BoxV1", "BoxBadReorder"
    )
    assert not report.compatible
    assert "SLOT_MOVED" in codes(report)


def test_compiled_type_widening_rejected(compiled_artifacts):
    report = check_layouts(get_layout("WidenV1"), get_layout("WidenV2"), "WidenV1", "WidenV2")
    assert not report.compatible
    assert "TYPE_CHANGED" in codes(report)


def test_compiled_base_class_change_rejected(compiled_artifacts):
    report = check_layouts(
        get_layout("BaseV1"),
        get_layout("BaseV2"),
        "BaseV1",
        "BaseV2",
        old_bases=get_base_contracts("BaseV1"),
        new_bases=get_base_contracts("BaseV2"),
    )
    assert not report.compatible
    assert "VARIABLE_INSERTED" in codes(report)
    assert "SLOT_MOVED" in codes(report)
    assert "INHERITANCE_CHANGED" in codes(report)


def test_compiled_dynamic_type_change_rejected(compiled_artifacts):
    report = check_layouts(
        get_layout("DynTypeV1"), get_layout("DynTypeV2"), "DynTypeV1", "DynTypeV2"
    )
    assert not report.compatible
    assert "TYPE_CHANGED" in codes(report)
