"""reducer 数值与失败类别测试。

答案来源三独立：
1. 手算常数（reference.hand_computed_constants，字面量）；
2. 测试内纯 Python 加权平均（conftest.explicit_weighted_average）；
3. 联合批参照（reference.union_batch_gradient）。
"""

from __future__ import annotations

import numpy as np
import pytest

from gradbucket.reducer import (
    BucketShapeError,
    Contribution,
    DuplicateContributionError,
    NonFiniteGradientError,
    PartialSlotMaskError,
    SampleCountError,
    reduce_bucket,
)
from gradbucket.reference import hand_computed_constants
from gradbucket.tensors import ParamSpec, build_layout

from conftest import explicit_weighted_average


def layout_1param():
    return build_layout(0, [ParamSpec("g", (1,))], bucket_capacity=1)


def contrib(worker, vec, mask, n, rid="r"):
    return Contribution(worker, np.asarray(vec, dtype=np.float64),
                        np.asarray(mask, dtype=bool), n, rid)


# ---------------------------------------------------------------------------
# 手算常数：不等批量加权 vs 按工作者数平均
# ---------------------------------------------------------------------------


def test_hand_constants_match_weighted_reduction_not_naive_average():
    const = hand_computed_constants()
    d = const["discriminator"]
    layout = layout_1param()
    # bias 槽同样是 1 维，这里只放一个参数的桶即可。
    counts = const["shard_sizes"]
    worker_g = d["worker_gw"]
    cs = [
        contrib(f"w{i}", [worker_g[i]], [True], counts[i])
        for i in range(3)
    ]
    red = reduce_bucket(layout, 0, cs)
    result = red.vec[0]
    # 具体数值断言（手算），不是"接口能调用"。
    assert result == pytest.approx(d["weighted_gw"], abs=0.0)
    assert result == pytest.approx(d["union_gw"], abs=0.0)
    # 关键反例：按人头平均会得到另一个值，reducer 必须与其不同。
    naive = d["naive_worker_average_gw"]
    assert abs(result - naive) > 0.8
    assert red.slots[0].weight_total == pytest.approx(4.0)
    assert red.slots[0].contributor_counts == (1, 2, 1)


def test_hand_constants_general_case_literals():
    const = hand_computed_constants()
    g = const["general"]
    layout = layout_1param()
    cs = [
        contrib(f"w{i}", [g["worker_gw"][i]], [True], const["shard_sizes"][i])
        for i in range(3)
    ]
    assert reduce_bucket(layout, 0, cs).vec[0] == pytest.approx(-2.5, abs=0.0)


# ---------------------------------------------------------------------------
# 完成顺序无关：不同提交顺序逐位一致
# ---------------------------------------------------------------------------


def test_result_is_bit_identical_regardless_of_arrival_order():
    layout = build_layout(
        0, [ParamSpec("a", (3,)), ParamSpec("b", (2,))], bucket_capacity=2
    )
    rng = np.random.default_rng(0)
    raw = {w: (rng.normal(size=5), rng.integers(1, 6)) for w in ["x", "y", "z"]}
    cs = [contrib(w, v, [True] * 5, n) for w, (v, n) in raw.items()]
    base = reduce_bucket(layout, 0, cs).vec
    for order in [(2, 0, 1), (1, 2, 0), (0, 2, 1), (2, 1, 0)]:
        shuffled = [cs[i] for i in order]
        out = reduce_bucket(layout, 0, shuffled).vec
        assert np.array_equal(out, base)


def test_matches_independent_python_oracle_with_unequal_counts():
    layout = build_layout(
        0, [ParamSpec("a", (3,)), ParamSpec("b", (2,))], bucket_capacity=2
    )
    pairs = [
        (2, np.array([1.0, 2.0, 3.0, 10.0, -10.0])),
        (5, np.array([4.0, 2.0, 8.0, 0.0, 1.0])),
        (1, np.array([-3.0, 0.0, 0.0, 4.0, 4.0])),
    ]
    cs = [contrib(f"w{i}", v, [True] * 5, n) for i, (n, v) in enumerate(pairs)]
    got = reduce_bucket(layout, 0, cs).vec
    expected = explicit_weighted_average(pairs)
    np.testing.assert_allclose(got, expected, rtol=0, atol=1e-12)
    # 分母必须是 8（真实样本总数），不是 3（工作者数）。
    assert all(s.weight_total == pytest.approx(8.0) for s in
               reduce_bucket(layout, 0, cs).slots)


# ---------------------------------------------------------------------------
# 缺梯度：显式占位，逐槽剔除，分母逐槽不同
# ---------------------------------------------------------------------------


def test_placeholder_slot_is_excluded_from_that_slots_denominator():
    layout = build_layout(
        0, [ParamSpec("a", (2,)), ParamSpec("b", (1,))], bucket_capacity=2
    )
    c0 = contrib("w0", [1.0, 1.0, 5.0], [True, True, True], 1)
    c1 = contrib("w1", [3.0, 3.0, 7.0], [True, True, True], 3)
    # w2 没算 a：a 槽零占位 mask False；b 仍然真实提交。
    c2 = contrib("w2", [0.0, 0.0, 9.0], [False, False, True], 4)
    red = reduce_bucket(layout, 0, [c0, c1, c2])

    slot_a, slot_b = red.slots
    # a 只由 w0(n=1),w1(n=3) 覆盖：(1*1+3*3)/4 = 2.5；w2 的样本被剔除。
    np.testing.assert_allclose(red.vec[:2], [2.5, 2.5], rtol=0, atol=1e-12)
    assert slot_a.covered is True
    assert slot_a.contributor_ids == ("w0", "w1")
    assert slot_a.weight_total == pytest.approx(4.0)
    # b 三个人都覆盖：(1*5+3*7+4*9)/8 = 7.75，分母与 a 不同。
    assert red.vec[2] == pytest.approx(7.75, abs=1e-12)
    assert slot_b.weight_total == pytest.approx(8.0)
    # 独立预言机复核 a 槽。
    expected_a = explicit_weighted_average(
        [(1, np.array([1.0, 1.0])), (3, np.array([3.0, 3.0]))]
    )
    np.testing.assert_allclose(red.vec[:2], expected_a, rtol=0, atol=1e-12)


def test_slot_with_zero_evidence_is_marked_uncovered_not_silently_zero():
    layout = build_layout(0, [ParamSpec("a", (2,))], bucket_capacity=1)
    c0 = contrib("w0", [0.0, 0.0], [False, False], 2)
    c1 = contrib("w1", [0.0, 0.0], [False, False], 5)
    red = reduce_bucket(layout, 0, [c0, c1])
    assert red.slots[0].covered is False
    assert red.all_slots_covered is False
    assert red.slots[0].contributor_ids == ()
    np.testing.assert_array_equal(red.vec, np.zeros(2))


def test_placeholder_zero_values_do_not_infiltrate_average_even_if_masked_true_fails():
    # 防御性边界：占位若被错误标成 True，就会以零值参与分母——
    # 这是本设计明确要避免的；通过 mask 语义测试固定该行为。
    layout = build_layout(0, [ParamSpec("a", (1,))], bucket_capacity=1)
    real = contrib("w0", [10.0], [True], 1)
    wrongly_unmasked_zero = contrib("w1", [0.0], [True], 100)
    red = reduce_bucket(layout, 0, [real, wrongly_unmasked_zero])
    # mask=True 即视为真实梯度，零也是合法梯度，结果被大样本数拉低。
    assert red.vec[0] == pytest.approx(10.0 / 101.0, abs=1e-12)
    # 对照：正确占位（mask=False）时 w1 被逐槽剔除。
    proper_placeholder = contrib("w1", [0.0], [False], 100)
    red2 = reduce_bucket(layout, 0, [real, proper_placeholder])
    assert red2.vec[0] == pytest.approx(10.0, abs=1e-12)
    assert red2.slots[0].covered is True


# ---------------------------------------------------------------------------
# 失败类别
# ---------------------------------------------------------------------------


def test_wrong_vector_length_is_rejected_with_stable_code():
    layout = layout_1param()
    c = contrib("w0", [1.0, 2.0], [True, True], 2)
    with pytest.raises(BucketShapeError) as ei:
        reduce_bucket(layout, 0, [c])
    assert ei.value.code == "REJECTED_BUCKET_SHAPE"


def test_mask_shape_mismatch_rejected():
    layout = layout_1param()
    c = Contribution("w0", np.zeros(1), np.zeros(2, dtype=bool), 2, "r")
    with pytest.raises(BucketShapeError):
        reduce_bucket(layout, 0, [c])


def test_partial_slot_mask_is_rejected():
    layout = build_layout(0, [ParamSpec("a", (3,))], bucket_capacity=1)
    c = contrib("w0", [1.0, 2.0, 3.0], [True, False, True], 2)
    with pytest.raises(PartialSlotMaskError) as ei:
        reduce_bucket(layout, 0, [c])
    assert ei.value.code == "REJECTED_PARTIAL_SLOT_MASK"


def test_nonpositive_sample_count_rejected():
    layout = layout_1param()
    for bad_n in (0, -3):
        c = contrib("w0", [1.0], [True], bad_n)
        with pytest.raises(SampleCountError) as ei:
            reduce_bucket(layout, 0, [c])
        assert ei.value.code == "REJECTED_SAMPLE_COUNT"


def test_bool_sample_count_rejected_not_treated_as_one():
    # bool 是 int 子类，必须显式拒绝，防止 True 被当作样本数 1。
    layout = layout_1param()
    c = Contribution("w0", np.array([1.0]), np.array([True]), True, "r")
    with pytest.raises(SampleCountError, match="布尔"):
        reduce_bucket(layout, 0, [c])


def test_sample_count_above_exact_integer_bound_rejected():
    from gradbucket.reducer import MAX_SAMPLE_COUNT
    layout = layout_1param()
    c = contrib("w0", [1.0], [True], MAX_SAMPLE_COUNT + 1)
    with pytest.raises(SampleCountError, match="2\\^53"):
        reduce_bucket(layout, 0, [c])
    # 恰好在界内可用。
    c_ok = contrib("w0", [1.0], [True], MAX_SAMPLE_COUNT)
    assert reduce_bucket(layout, 0, [c_ok]).vec[0] == pytest.approx(1.0)


def test_non_finite_weighted_result_rejected_even_with_finite_inputs():
    # 有限输入 × 极大样本数也可能在中间累加溢出；结果必须被拦下而非产出 NaN。
    # 用受 2^53 上界约束后仍可能溢出的极端值：梯度本身很大。
    layout = build_layout(0, [ParamSpec("a", (1,))], bucket_capacity=1)
    big = 2 ** 53
    c1 = contrib("w0", [1e300], [True], big)
    c2 = contrib("w1", [1e300], [True], big)
    with pytest.raises(NonFiniteGradientError) as ei:
        reduce_bucket(layout, 0, [c1, c2])
    assert ei.value.code == "REJECTED_NON_FINITE"


def test_nan_in_placeholder_region_is_allowed_real_region_still_validated():
    # 占位区（mask=False）的值不进入归约，允许含 NaN；真实区仍严格校验。
    layout = build_layout(0, [ParamSpec("a", (1,)), ParamSpec("b", (1,))], 2)
    c_placeholder_nan = contrib("w0", [float("nan"), 5.0], [False, True], 2)
    c_other = contrib("w1", [3.0, 7.0], [True, True], 2)
    red = reduce_bucket(layout, 0, [c_placeholder_nan, c_other])
    # a 槽仅 w1 覆盖 -> 3.0；b 槽两者覆盖 -> 6.0。
    np.testing.assert_allclose(red.vec, [3.0, 6.0])
    bad = contrib("w0", [float("nan"), 5.0], [True, True], 2)
    with pytest.raises(NonFiniteGradientError):
        reduce_bucket(layout, 0, [bad, c_other])


def test_duplicate_worker_rejected():
    layout = layout_1param()
    c1 = contrib("w0", [1.0], [True], 2)
    c2 = contrib("w0", [2.0], [True], 2)
    with pytest.raises(DuplicateContributionError) as ei:
        reduce_bucket(layout, 0, [c1, c2])
    assert ei.value.code == "REJECTED_DUPLICATE"


def test_non_finite_gradient_rejected():
    layout = layout_1param()
    c = contrib("w0", [float("nan")], [True], 2)
    with pytest.raises(NonFiniteGradientError) as ei:
        reduce_bucket(layout, 0, [c])
    assert ei.value.code == "REJECTED_NON_FINITE"
    c2 = contrib("w0", [float("inf")], [True], 2)
    with pytest.raises(NonFiniteGradientError):
        reduce_bucket(layout, 0, [c2])


def test_empty_contribution_set_is_uncovered_not_average_of_nothing():
    layout = layout_1param()
    red = reduce_bucket(layout, 0, [])
    assert red.all_slots_covered is False
    assert red.vec.tolist() == [0.0]
