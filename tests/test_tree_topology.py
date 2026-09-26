"""TransformTree：多父冲突、环检测、不连通森林、跨分支 LCA 路径。"""

import numpy as np
import pytest

from transform_tree.errors import (
    CycleDetectedError,
    FramesNotConnectedError,
    MultipleParentsError,
    UnknownFrameError,
)
from transform_tree.timed_sequence import StaticTransformProvider
from transform_tree.transform import Transform
from transform_tree.tree import TransformTree


def _static(t=(0, 0, 0)):
    return StaticTransformProvider(Transform(np.eye(3), np.asarray(t, dtype=float)))


def test_add_second_parent_is_rejected():
    tree = TransformTree()
    tree.add_edge("a", "b", _static())
    with pytest.raises(MultipleParentsError):
        tree.add_edge("c", "b", _static())
    # 冲突边未生效：b 的父仍是 a。
    assert tree.chain_frames("a", "b") == ["a", "b"]


def test_repeated_edge_same_parent_is_allowed_as_idempotent():
    tree = TransformTree()
    tree.add_edge("a", "b", _static((1, 0, 0)))
    tree.add_edge("a", "b", _static((2, 0, 0)))  # 同一父，视为幂等更新
    t = tree.lookup_transform("a", "b", 0.0)
    np.testing.assert_allclose(t.translation, [2, 0, 0])


def test_self_loop_is_rejected():
    tree = TransformTree()
    with pytest.raises(CycleDetectedError):
        tree.add_edge("a", "a", _static())


def test_cycle_is_rejected_when_linking_back_to_ancestor():
    tree = TransformTree()
    tree.add_edge("a", "b", _static())
    tree.add_edge("b", "c", _static())
    # 让 a 的父变成 c 会形成 a->b->c->a 的环。
    with pytest.raises(CycleDetectedError):
        tree.add_edge("c", "a", _static())


def test_deep_cycle_is_detected():
    tree = TransformTree()
    tree.add_edge("a", "b", _static())
    tree.add_edge("b", "c", _static())
    tree.add_edge("c", "d", _static())
    with pytest.raises(CycleDetectedError):
        tree.add_edge("d", "a", _static())


def test_unknown_frame_lookup_is_rejected():
    tree = TransformTree()
    tree.add_edge("a", "b", _static())
    with pytest.raises(UnknownFrameError):
        tree.lookup_transform("a", "ghost", 0.0)


def test_disconnected_forest_raises():
    tree = TransformTree()
    tree.add_edge("a", "b", _static())
    tree.add_edge("x", "y", _static())
    with pytest.raises(FramesNotConnectedError):
        tree.lookup_transform("b", "y", 0.0)


def test_same_frame_query_returns_identity():
    tree = TransformTree()
    tree.add_frame("lone")
    t = tree.lookup_transform("lone", "lone", 123.0)
    np.testing.assert_allclose(t.to_matrix(), np.eye(4))


def test_path_across_branches_goes_through_lca():
    # root -> a -> leaf1 ; root -> b -> leaf2
    tree = TransformTree()
    tree.add_edge("root", "a", _static())
    tree.add_edge("a", "leaf1", _static((1, 0, 0)))
    tree.add_edge("root", "b", _static())
    tree.add_edge("b", "leaf2", _static((0, 1, 0)))
    chain = tree.chain_frames("leaf1", "leaf2")
    assert chain == ["leaf1", "a", "root", "b", "leaf2"]


def test_transform_across_branches_matches_manual_composition():
    tree = TransformTree()
    # T_root_a：平移 (1,0,0)；T_a_leaf1：平移 (0,1,0)
    tree.add_edge("root", "a", _static((1, 0, 0)))
    tree.add_edge("a", "leaf1", _static((0, 1, 0)))
    # T_root_b：Rz90；T_b_leaf2：平移 (0,0,2)
    rz90 = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)
    tree.add_edge("root", "b", StaticTransformProvider(Transform(rz90, np.zeros(3))))
    tree.add_edge("b", "leaf2", _static((0, 0, 2)))

    t = tree.lookup_transform("leaf1", "leaf2", 0.0)
    # 手算：
    # T_leaf1_root = inv(T_root_a * T_a_leaf1)
    #   复合：t = (1,0,0)+(0,1,0)=(1,1,0)，R=I；逆：R=I，t=(-1,-1,0)
    # T_root_leaf2 = Rz90, t = Rz90·(0,0,2) = (0,0,2)
    # 合计 T_leaf1_leaf2：R=Rz90，t = (-1,-1,0) + (0,0,2) = (-1,-1,2)
    np.testing.assert_allclose(t.rotation, rz90, atol=1e-12)
    np.testing.assert_allclose(t.translation, [-1, -1, 2], atol=1e-12)


def test_chain_frames_refuses_unknown_frames():
    tree = TransformTree()
    tree.add_edge("a", "b", _static())
    with pytest.raises(UnknownFrameError):
        tree.chain_frames("a", "nope")
