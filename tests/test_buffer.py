"""Tests for the per-edge time buffer: interpolation, SLERP continuity
across quaternion sign flips, static edges, and extrapolation rejection."""

import numpy as np
import pytest

from tf_cache import SE3, ExtrapolationError, LookupError_, TransformBuffer

SQRT2_2 = np.sqrt(2.0) / 2.0
Q_Z90 = np.array([0.0, 0.0, SQRT2_2, SQRT2_2])


def make(quat, trans):
    return SE3.from_quat_translation(quat, trans)


def test_empty_buffer_raises():
    with pytest.raises(LookupError_):
        TransformBuffer().lookup(0.0)


def test_single_sample_is_static():
    buf = TransformBuffer()
    buf.insert(5.0, make(Q_Z90, [1.0, 2.0, 3.0]))
    for t in (-100.0, 0.0, 5.0, 1e6):
        got = buf.lookup(t)
        np.testing.assert_allclose(got.translation, [1.0, 2.0, 3.0], atol=1e-12)
        assert got.error_to(make(Q_Z90, [1.0, 2.0, 3.0]))[1] < 1e-12


def test_linear_interpolation_midpoint():
    buf = TransformBuffer()
    buf.insert(0.0, make([0, 0, 0, 1], [0.0, 0.0, 0.0]))
    buf.insert(2.0, make(Q_Z90, [2.0, 4.0, 6.0]))
    mid = buf.lookup(1.0)
    np.testing.assert_allclose(mid.translation, [1.0, 2.0, 3.0], atol=1e-12)
    assert mid.rotation.magnitude() == pytest.approx(np.pi / 4.0, abs=1e-12)


def test_out_of_order_insert_keeps_sorted():
    buf = TransformBuffer()
    buf.insert(2.0, make([0, 0, 0, 1], [2.0, 0.0, 0.0]))
    buf.insert(0.0, make([0, 0, 0, 1], [0.0, 0.0, 0.0]))
    buf.insert(1.0, make([0, 0, 0, 1], [1.0, 0.0, 0.0]))
    assert buf.times == [0.0, 1.0, 2.0]
    np.testing.assert_allclose(buf.lookup(0.5).translation, [0.5, 0.0, 0.0], atol=1e-12)


def test_same_time_insert_replaces():
    buf = TransformBuffer()
    buf.insert(1.0, make([0, 0, 0, 1], [0.0, 0.0, 0.0]))
    buf.insert(1.0, make([0, 0, 0, 1], [9.0, 9.0, 9.0]))
    assert len(buf) == 1
    np.testing.assert_allclose(buf.lookup(1.0).translation, [9.0, 9.0, 9.0], atol=1e-12)


def test_extrapolation_rejected_both_sides():
    buf = TransformBuffer()
    buf.insert(1.0, make([0, 0, 0, 1], [0.0, 0.0, 0.0]))
    buf.insert(2.0, make([0, 0, 0, 1], [1.0, 0.0, 0.0]))
    with pytest.raises(ExtrapolationError):
        buf.lookup(0.999)
    with pytest.raises(ExtrapolationError):
        buf.lookup(2.001)
    # Boundaries themselves are fine.
    buf.lookup(1.0)
    buf.lookup(2.0)


def test_sign_flip_continuity_across_samples():
    """Keyframes q at t=0 and -q at t=1 describe one constant rotation."""
    buf = TransformBuffer()
    buf.insert(0.0, make(Q_Z90, [0.0, 0.0, 0.0]))
    buf.insert(1.0, make(-Q_Z90, [0.0, 0.0, 0.0]))
    reference = make(Q_Z90, [0.0, 0.0, 0.0])
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        _, d_rot = buf.lookup(t).error_to(reference)
        assert d_rot < 1e-12
