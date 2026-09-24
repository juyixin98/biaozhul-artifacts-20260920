"""Tests for the transform tree: multi-edge composition, inverse traversal,
cycle rejection, missing chains, asynchronous sampling, and round-trip error."""

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from tf_cache import (
    SE3,
    ConnectivityError,
    CycleError,
    ExtrapolationError,
    TransformTree,
)

SQRT2_2 = np.sqrt(2.0) / 2.0
Q_Z90 = np.array([0.0, 0.0, SQRT2_2, SQRT2_2])


def make(quat, trans):
    return SE3.from_quat_translation(quat, trans)


def build_chain() -> TransformTree:
    """world -> base -> camera with static edges."""
    tree = TransformTree()
    tree.set_transform("world", "base", 0.0, make(Q_Z90, [1.0, 0.0, 0.0]))
    tree.set_transform("base", "camera", 0.0, make([0, 0, 0, 1], [0.5, 0.0, 0.2]))
    return tree


def test_multi_edge_composition():
    tree = build_chain()
    t_ab = make(Q_Z90, [1.0, 0.0, 0.0])
    t_bc = make([0, 0, 0, 1], [0.5, 0.0, 0.2])
    expected = t_ab * t_bc
    got = tree.lookup("camera", "world", 0.0)
    d_trans, d_rot = got.error_to(expected)
    assert d_trans < 1e-12
    assert d_rot < 1e-12


def test_inverse_traversal():
    tree = build_chain()
    forward = tree.lookup("camera", "world", 0.0)
    backward = tree.lookup("world", "camera", 0.0)
    d_trans, d_rot = (forward * backward).error_to(SE3.identity())
    assert d_trans < 1e-12
    assert d_rot < 1e-12


def test_lookup_same_frame_is_identity():
    tree = build_chain()
    got = tree.lookup("base", "base", 123.0)
    d_trans, d_rot = got.error_to(SE3.identity())
    assert d_trans == 0.0
    assert d_rot == 0.0


def test_cycle_rejected():
    tree = build_chain()
    with pytest.raises(CycleError):
        tree.set_transform("camera", "world", 0.0, make([0, 0, 0, 1], [0, 0, 0]))
    with pytest.raises(CycleError):
        tree.set_transform("base", "base", 0.0, make([0, 0, 0, 1], [0, 0, 0]))


def test_second_parent_rejected():
    tree = build_chain()
    with pytest.raises(CycleError):
        tree.set_transform("odom", "camera", 0.0, make([0, 0, 0, 1], [0, 0, 0]))


def test_missing_chain_rejected():
    tree = build_chain()
    tree.set_transform("other", "lidar", 0.0, make([0, 0, 0, 1], [0, 0, 0]))
    with pytest.raises(ConnectivityError):
        tree.lookup("camera", "lidar", 0.0)
    with pytest.raises(ConnectivityError):
        tree.lookup("camera", "nonexistent", 0.0)


def test_extrapolation_rejected_through_chain():
    tree = TransformTree()
    tree.set_transform("a", "b", 0.0, make([0, 0, 0, 1], [0, 0, 0]))
    tree.set_transform("a", "b", 1.0, make([0, 0, 0, 1], [1, 0, 0]))
    tree.set_transform("b", "c", 0.0, make([0, 0, 0, 1], [0, 1, 0]))
    tree.set_transform("b", "c", 1.0, make([0, 0, 0, 1], [0, 2, 0]))
    with pytest.raises(ExtrapolationError):
        tree.lookup("c", "a", 1.5)


def test_asynchronous_sampling():
    """Edges sampled at different rates/times still compose at a query time."""
    tree = TransformTree()
    # Edge a->b sampled sparsely: translation moves 0 -> 2 along x over [0, 2].
    tree.set_transform("a", "b", 0.0, make([0, 0, 0, 1], [0.0, 0.0, 0.0]))
    tree.set_transform("a", "b", 2.0, make([0, 0, 0, 1], [2.0, 0.0, 0.0]))
    # Edge b->c sampled densely and at non-coincident times: 0 -> 90 deg about z.
    for t, frac in ((0.0, 0.0), (0.7, 0.35), (1.3, 0.65), (2.0, 1.0)):
        angle = frac * np.pi / 2.0
        q = [0.0, 0.0, np.sin(angle / 2.0), np.cos(angle / 2.0)]
        tree.set_transform("b", "c", t, make(q, [0.0, 1.0 * frac, 0.0]))

    got = tree.lookup("c", "a", 1.0)

    # Expected: interpolate each edge independently at t=1.0, then compose.
    t_ab = make([0, 0, 0, 1], [1.0, 0.0, 0.0])
    q45 = [0.0, 0.0, np.sin(np.pi / 8.0), np.cos(np.pi / 8.0)]
    t_bc = make(q45, [0.0, 0.5, 0.0])
    expected = t_ab * t_bc
    d_trans, d_rot = got.error_to(expected)
    assert d_trans < 1e-12
    assert d_rot < 1e-12


def test_compose_then_inverse_roundtrip_error():
    """lookup(a->c) * lookup(c->a) must be the identity to numerical noise."""
    rng = np.random.default_rng(1234)
    tree = TransformTree()
    frames = ["f0", "f1", "f2", "f3", "f4"]
    for t in (0.0, 0.3, 0.9, 1.7):
        for parent, child in zip(frames, frames[1:]):
            rot = Rotation.random(random_state=rng)
            tree.set_transform(parent, child, t, SE3(rot, rng.normal(size=3)))
    for t in (0.0, 0.15, 0.5, 1.0, 1.7):
        fwd = tree.lookup("f4", "f0", t)
        inv = tree.lookup("f0", "f4", t)
        d_trans, d_rot = (fwd * inv).error_to(SE3.identity())
        assert d_trans < 1e-10
        assert d_rot < 1e-10
