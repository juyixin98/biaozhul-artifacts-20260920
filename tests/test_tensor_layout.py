"""固定桶布局 / 槽位 / 打包解包测试。"""

from __future__ import annotations

import numpy as np
import pytest

from gradbucket.tensors import (
    ParamSpec,
    build_layout,
    empty_payload,
    pack_bucket,
    placeholder_for,
    unpack_bucket,
)


def make_specs():
    return [
        ParamSpec("w", (2, 3)),
        ParamSpec("b", (2,)),
        ParamSpec("spare", (2,)),
    ]


def test_layout_offsets_follow_declaration_order():
    layout = build_layout(0, make_specs(), bucket_capacity=2)
    assert layout.bucket_sizes == (2, 1)
    assert [(s.index, s.param.name, s.offset, s.length) for s in layout.slots] == [
        (0, "w", 0, 6),
        (1, "b", 6, 2),
        (2, "spare", 8, 2),
    ]
    assert layout.total_length == 10
    assert [s.param.name for s in layout.slots_for_bucket(0)] == ["w", "b"]
    assert [s.param.name for s in layout.slots_for_bucket(1)] == ["spare"]


def test_layout_is_identical_when_params_same():
    a = build_layout(7, make_specs(), bucket_capacity=2)
    b = build_layout(7, make_specs(), bucket_capacity=2)
    assert a.describe() == b.describe()
    with pytest.raises(Exception):  # frozen=True
        a.slots = ()  # type: ignore[misc]


def test_duplicate_param_name_rejected():
    with pytest.raises(ValueError, match="重复"):
        build_layout(0, [ParamSpec("w", (2,)), ParamSpec("w", (2,))], 2)


def test_capacity_larger_than_params_single_bucket():
    layout = build_layout(0, make_specs(), bucket_capacity=10)
    assert layout.bucket_sizes == (3,)


def test_pack_requires_every_slot_no_implicit_drop():
    layout = build_layout(0, make_specs(), 2)
    values = {"w": np.zeros((2, 3)), "b": np.zeros(2)}  # 缺 spare 不在此桶
    packed = pack_bucket(layout, 0, values)
    assert packed.shape == (8,)
    with pytest.raises(KeyError, match="b"):
        pack_bucket(layout, 0, {"w": np.zeros((2, 3))})


def test_pack_shape_mismatch_rejected():
    layout = build_layout(0, make_specs(), 2)
    with pytest.raises(ValueError, match="形状不匹配"):
        pack_bucket(layout, 0, {"w": np.zeros(2), "b": np.zeros(2)})


def test_pack_unpack_roundtrip_preserves_values_and_order():
    layout = build_layout(0, make_specs(), 2)
    w = np.arange(6, dtype=np.float64).reshape(2, 3)
    b = np.array([9.0, -9.0])
    packed = pack_bucket(layout, 0, {"w": w, "b": b})
    # 槽位顺序固定：w 的 6 个元素在前，b 在后。
    assert packed.tolist() == [0, 1, 2, 3, 4, 5, 9.0, -9.0]
    out = unpack_bucket(layout, 0, packed)
    assert np.array_equal(out["w"], w)
    assert np.array_equal(out["b"], b)


def test_wrong_bucket_vector_length_rejected_on_unpack():
    layout = build_layout(0, make_specs(), 2)
    with pytest.raises(ValueError, match="长度"):
        unpack_bucket(layout, 1, np.zeros(99))


def test_placeholder_is_correct_shape_zero_and_distinct():
    layout = build_layout(0, make_specs(), 2)
    slot = layout.slot_by_name("b")
    ph = placeholder_for(slot)
    assert ph.shape == (2,)
    assert np.all(ph == 0.0)
    ph[0] = 7.0  # 占位是独立副本，不污染布局
    assert np.all(placeholder_for(slot) == 0.0)


def test_empty_payload_marks_explicit_placeholders():
    layout = build_layout(0, make_specs(), 2)
    vec, mask = empty_payload(layout, 0, present=["w"])
    assert vec.shape == (8,) and mask.shape == (8,)
    assert mask.tolist() == [True] * 6 + [False] * 2
