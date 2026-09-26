"""手算验收：三层变换链、逆变换、时刻缺口、旋转插值、时间覆盖边界。

手算模型（所有旋转均绕 z 轴，便于纸面计算）：
    T_base_arm   ：Rz(90°)，t = (1, 0, 0)
    T_arm_wrist  ：Rz(90°)，t = (0, 1, 0)

复合 T_base_wrist = T_base_arm * T_arm_wrist：
    R = Rz(90°) @ Rz(90°) = Rz(180°)
    t = Rz(90°)·(0,1,0) + (1,0,0) = (-1,0,0) + (1,0,0) = (0,0,0)
    点 p_wrist = (1,0,0) -> p_base = Rz(180°)·(1,0,0) = (-1,0,0)

逆 T_wrist_base：R = Rz(180°)（自逆），t = (0,0,0)。
单段逆 T_wrist_arm = (T_arm_wrist)^-1：
    R = Rz(-90°)
    t = -Rz(-90°)·(0,1,0) = -(1,0,0) = (-1,0,0)
"""

import numpy as np
import pytest

from transform_tree.errors import TimeGapError, TimeNotCoveredError
from transform_tree.timed_sequence import Keyframe, StaticTransformProvider, TimedTransformSequence
from transform_tree.transform import Transform
from transform_tree.tree import TransformTree

_RZ90 = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)
_RZ180 = np.array([[-1, 0, 0], [0, -1, 0], [0, 0, 1]], dtype=float)
_RZ270 = np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 1]], dtype=float)


def _build_three_layer_tree() -> TransformTree:
    # 两段都给出 t=0 与 t=2 两帧，t=1 时旋转插值到 45°，用于插值验收。
    def seq(angle0, angle1, trans):
        return TimedTransformSequence(
            [
                Keyframe(0.0, Transform(np.eye(3), np.zeros(3))),
                Keyframe(2.0, Transform(angle1, np.asarray(trans, dtype=float))),
            ]
        )

    tree = TransformTree()
    tree.add_edge(
        "base",
        "arm",
        TimedTransformSequence(
            [
                Keyframe(0.0, Transform(np.eye(3), np.zeros(3))),
                Keyframe(2.0, Transform(_RZ90, np.array([1.0, 0.0, 0.0]))),
            ]
        ),
    )
    tree.add_edge(
        "arm",
        "wrist",
        TimedTransformSequence(
            [
                Keyframe(0.0, Transform(np.eye(3), np.zeros(3))),
                Keyframe(2.0, Transform(_RZ90, np.array([0.0, 1.0, 0.0]))),
            ]
        ),
    )
    tree.add_edge("wrist", "tip", StaticTransformProvider(Transform(np.eye(3), np.array([0.0, 0.0, 0.5]))))
    return tree


def test_hand_calculated_three_layer_chain():
    tree = _build_three_layer_tree()
    t = tree.lookup_transform("base", "wrist", 2.0)

    np.testing.assert_allclose(t.rotation, _RZ180, atol=1e-12)
    np.testing.assert_allclose(t.translation, [0, 0, 0], atol=1e-12)
    # 点变换：p_wrist=(1,0,0) -> p_base=(-1,0,0)
    np.testing.assert_allclose(t.transform_point([1, 0, 0]), [-1, 0, 0], atol=1e-12)


def test_hand_calculated_inverse_chain():
    tree = _build_three_layer_tree()
    inv = tree.lookup_transform("wrist", "base", 2.0)
    np.testing.assert_allclose(inv.rotation, _RZ180, atol=1e-12)
    np.testing.assert_allclose(inv.translation, [0, 0, 0], atol=1e-12)

    # 正反映射抵消。
    fwd = tree.lookup_transform("base", "wrist", 2.0)
    np.testing.assert_allclose((inv @ fwd).to_matrix(), np.eye(4), atol=1e-12)


def test_hand_calculated_single_edge_inverse():
    tree = _build_three_layer_tree()
    t = tree.lookup_transform("wrist", "arm", 2.0)
    np.testing.assert_allclose(t.rotation, _RZ270, atol=1e-12)
    np.testing.assert_allclose(t.translation, [-1, 0, 0], atol=1e-12)


def test_full_chain_includes_static_tip():
    # base -> arm -> wrist -> tip：静态末端在 wrist 上方 z=0.5；t=2 时
    # p_tip=(0,0,0) 经 T_wrist_tip 得 (0,0,0.5)，再经 Rz180 平移为 0，结果 z 不变。
    tree = _build_three_layer_tree()
    chain = tree.chain_frames("base", "tip")
    assert chain == ["base", "arm", "wrist", "tip"]
    t = tree.lookup_transform("base", "tip", 2.0)
    np.testing.assert_allclose(t.rotation, _RZ180, atol=1e-12)
    np.testing.assert_allclose(t.transform_point([0, 0, 0]), [0, 0, 0.5], atol=1e-12)


def test_rotation_interpolation_at_midpoint():
    # t=1 时两层各插值到 Rz(45°)，复合为 Rz(90°)，平移各为手算值的一半：
    # t_base_arm = (0.5,0,0)，t_arm_wrist = (0,0.5,0)
    # 复合 t = Rz45·(0,0.5,0) + (0.5,0,0)
    tree = _build_three_layer_tree()
    t = tree.lookup_transform("base", "wrist", 1.0)
    np.testing.assert_allclose(t.rotation, _RZ90, atol=1e-12)
    expected_t = _RZ_45 @ np.array([0.0, 0.5, 0.0]) + np.array([0.5, 0.0, 0.0])
    np.testing.assert_allclose(t.translation, expected_t, atol=1e-12)


def test_history_is_not_filled_with_latest_value():
    # 查询时刻早于首帧 / 晚于末帧都必须报错，绝不拿末帧值顶替。
    tree = _build_three_layer_tree()
    for bad_time in (-1.0, 2.5):
        with pytest.raises(TimeNotCoveredError):
            tree.lookup_transform("base", "wrist", bad_time)


def test_time_gap_propagates_through_chain():
    tree = TransformTree()
    tree.add_edge(
        "base",
        "arm",
        TimedTransformSequence(
            [
                Keyframe(0.0, Transform(np.eye(3), np.zeros(3))),
                Keyframe(1.0, Transform(_RZ90, np.array([1.0, 0.0, 0.0]))),
                Keyframe(4.0, Transform(_RZ90, np.array([1.0, 0.0, 0.0]))),
            ],
            max_gap=0.6,
        ),
    )
    tree.add_edge("arm", "wrist", StaticTransformProvider(Transform(np.eye(3), np.zeros(3))))
    with pytest.raises(TimeGapError):
        tree.lookup_transform("base", "wrist", 2.0)


_RZ_45 = np.array(
    [[np.cos(np.pi / 4), -np.sin(np.pi / 4), 0], [np.sin(np.pi / 4), np.cos(np.pi / 4), 0], [0, 0, 1]]
)
