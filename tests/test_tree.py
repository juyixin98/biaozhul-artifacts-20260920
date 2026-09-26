"""TransformTree 的验收测试：三层链手算、逆变换、时刻缺口、拓扑校验。"""

import numpy as np
import pytest

from transform_tree import (
    ConnectivityError,
    CycleError,
    ExtrapolationError,
    MultiParentError,
    Transform,
    TransformTree,
    UnknownFrameError,
)
from transform_tree.quaternion import from_axis_angle

SQRT2_2 = np.sqrt(2.0) / 2.0
RZ90 = [0, 0, SQRT2_2, SQRT2_2]  # 绕 z 轴 +90°
IDENTITY = [0, 0, 0, 1]


def _three_level_tree() -> TransformTree:
    """world -> base -> arm -> tool，所有边在 t∈[0,10] 上为静态变换。"""
    tree = TransformTree()
    for t in (0.0, 10.0):
        tree.add_transform("world", "base", t, Transform([1, 0, 0], IDENTITY))
        tree.add_transform("base", "arm", t, Transform([0, 1, 0], RZ90))
        tree.add_transform("arm", "tool", t, Transform([0, 0, 1], IDENTITY))
    return tree


def test_three_level_chain_hand_computed():
    """手算：world_T_tool 平移 (1,1,1)，旋转 Rz(+90°)。"""
    tree = _three_level_tree()
    t = tree.lookup_transform("world", "tool", 5.0)
    np.testing.assert_allclose(t.translation, [1, 1, 1], atol=1e-12)
    np.testing.assert_allclose(t.rotation, RZ90, atol=1e-12)
    # 验证点映射：tool 系下 (1,0,0) -> world 系下 (1,2,1)
    np.testing.assert_allclose(t.apply([1, 0, 0]), [1, 2, 1], atol=1e-12)


def test_inverse_query_matches_hand_computed():
    """tool_T_world 应为 world_T_tool 的逆：平移 (-1,1,-1)，旋转 Rz(-90°)。"""
    tree = _three_level_tree()
    inv = tree.lookup_transform("tool", "world", 5.0)
    np.testing.assert_allclose(inv.translation, [-1, 1, -1], atol=1e-12)
    np.testing.assert_allclose(inv.rotation, [0, 0, -SQRT2_2, SQRT2_2], atol=1e-12)
    # 与正向查询组合应为恒等
    fwd = tree.lookup_transform("world", "tool", 5.0)
    identity = fwd.compose(inv)
    np.testing.assert_allclose(identity.translation, [0, 0, 0], atol=1e-12)
    np.testing.assert_allclose(identity.rotation, IDENTITY, atol=1e-12)


def test_mid_chain_query():
    """base_T_tool：平移 (0,1,1)，旋转 Rz(+90°)。"""
    tree = _three_level_tree()
    t = tree.lookup_transform("base", "tool", 5.0)
    np.testing.assert_allclose(t.translation, [0, 1, 1], atol=1e-12)
    np.testing.assert_allclose(t.rotation, RZ90, atol=1e-12)


def test_rotation_interpolated_at_half_time():
    """0°→90° 的边在 t=5（中点）查询应得到 45°。"""
    tree = TransformTree()
    tree.add_transform("world", "cam", 0.0, Transform([0, 0, 0], IDENTITY))
    tree.add_transform(
        "world", "cam", 10.0,
        Transform([0, 0, 0], from_axis_angle([0, 0, 1], np.pi / 2)),
    )
    t = tree.lookup_transform("world", "cam", 5.0)
    expected = from_axis_angle([0, 0, 1], np.pi / 4)
    np.testing.assert_allclose(t.rotation, expected, atol=1e-12)


def test_time_gap_on_one_edge_fails_whole_query():
    """路径上任一边在查询时刻无覆盖，整个查询必须失败。"""
    tree = TransformTree()
    for t in (0.0, 10.0):
        tree.add_transform("world", "base", t, Transform([1, 0, 0], IDENTITY))
    for t in (0.0, 4.0):  # base->arm 只覆盖到 t=4
        tree.add_transform("base", "arm", t, Transform([0, 1, 0], IDENTITY))
    with pytest.raises(ExtrapolationError):
        tree.lookup_transform("world", "arm", 5.0)
    # t=3 在两边覆盖范围内，应成功
    tree.lookup_transform("world", "arm", 3.0)


def test_query_beyond_latest_sample_does_not_return_latest():
    """不得把最新样本当作查询时刻的值。"""
    tree = _three_level_tree()
    with pytest.raises(ExtrapolationError):
        tree.lookup_transform("world", "tool", 10.5)


def test_query_before_earliest_sample_fails():
    tree = _three_level_tree()
    with pytest.raises(ExtrapolationError):
        tree.lookup_transform("world", "tool", -0.1)


def test_cycle_detected():
    tree = TransformTree()
    tree.add_transform("a", "b", 0.0, Transform([0, 0, 0], IDENTITY))
    tree.add_transform("b", "c", 0.0, Transform([0, 0, 0], IDENTITY))
    with pytest.raises(CycleError):
        tree.add_transform("c", "a", 0.0, Transform([0, 0, 0], IDENTITY))


def test_self_edge_is_cycle():
    tree = TransformTree()
    with pytest.raises(CycleError):
        tree.add_transform("a", "a", 0.0, Transform([0, 0, 0], IDENTITY))


def test_multi_parent_detected():
    tree = TransformTree()
    tree.add_transform("a", "b", 0.0, Transform([0, 0, 0], IDENTITY))
    with pytest.raises(MultiParentError):
        tree.add_transform("c", "b", 0.0, Transform([0, 0, 0], IDENTITY))


def test_disconnected_frames_fail():
    tree = TransformTree()
    tree.add_transform("a", "b", 0.0, Transform([0, 0, 0], IDENTITY))
    tree.add_transform("x", "y", 0.0, Transform([0, 0, 0], IDENTITY))
    with pytest.raises(ConnectivityError):
        tree.lookup_transform("a", "y", 0.0)


def test_unknown_frame_fails():
    tree = _three_level_tree()
    with pytest.raises(UnknownFrameError):
        tree.lookup_transform("world", "gripper", 5.0)


def test_same_frame_is_identity():
    tree = _three_level_tree()
    t = tree.lookup_transform("base", "base", 5.0)
    np.testing.assert_allclose(t.translation, [0, 0, 0], atol=1e-12)
    np.testing.assert_allclose(t.rotation, IDENTITY, atol=1e-12)


def test_sibling_frames_via_common_ancestor():
    """两个兄弟坐标系经公共祖先组合：world->a 与 world->b。"""
    tree = TransformTree()
    tree.add_transform("world", "a", 0.0, Transform([1, 0, 0], IDENTITY))
    tree.add_transform("world", "b", 0.0, Transform([0, 2, 0], IDENTITY))
    t = tree.lookup_transform("a", "b", 0.0)
    # a_T_b = inverse(world_T_a) ∘ world_T_b：平移 (-1,2,0)
    np.testing.assert_allclose(t.translation, [-1, 2, 0], atol=1e-12)
