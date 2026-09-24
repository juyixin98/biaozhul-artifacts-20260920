"""Geometry primitives: SDF, gradients, segment distance, densification."""
import numpy as np
import pytest

from app.geometry import (
    Rect,
    collapse_consecutive_duplicates,
    densify,
    expand_to_groups,
    rect_sdf_gradients,
    rect_signed_distance,
    segment_rect_distance,
    verify_trajectory,
)


@pytest.mark.parametrize(
    "point,expected",
    [
        ((3.0, 0.0), 1.0),     # right of box [0,2]x[-1,1]
        ((-1.0, 0.0), 1.0),    # left
        ((1.0, 2.0), 1.0),     # above
        ((1.0, -2.0), 1.0),    # below
        ((3.0, 2.0), np.sqrt(2)),  # outside corner
        ((1.0, 0.0), -1.0),    # inside: nearest face is x=2, distance 1
        ((0.0, 0.0), 0.0),     # corner on boundary
        ((2.0, 1.0), 0.0),     # corner on boundary
    ],
)
def test_signed_distance_values(point, expected):
    r = Rect(0.0, -1.0, 2.0, 1.0)
    assert rect_signed_distance(np.array([point]), r)[0] == pytest.approx(expected, abs=1e-12)


def test_sdf_gradients_finite_difference():
    rng = np.random.default_rng(42)
    r = Rect.from_center_wh(0.3, -0.2, 2.0, 1.0)
    S = np.vstack([
        rng.normal(scale=1.5, size=(200, 2)),
        rng.uniform([-0.6, -0.65], [1.2, 0.25], size=(80, 2)),
    ])
    g = rect_sdf_gradients(S, r)
    eps = 1e-7
    for j in range(S.shape[0]):
        for axis in range(2):
            Sp, Sm = S.copy(), S.copy()
            Sp[j, axis] += eps
            Sm[j, axis] -= eps
            fd = (rect_signed_distance(Sp, r)[j] - rect_signed_distance(Sm, r)[j]) / (2 * eps)
            assert g[j, axis] == pytest.approx(fd, abs=1e-5, rel=1e-5)


@pytest.mark.parametrize(
    "a,b,sign_expected",
    [
        ((-1, -2), (-1, 2), 1.0),       # parallel left of box, gap 1
        ((0, 0), (0, 1), 0.0),          # along left edge
        ((-1, 0), (3, 0), -1.0),        # through the box, endpoint depth 1
        ((3, -2), (3, 2), 1.0),         # parallel right, gap 1
        ((-1, 1), (1, -1), -0.5),         # deepest penetration -0.5 at (0.5,-0.5)
    ],
)
def test_segment_rect_distance(a, b, sign_expected):
    r = Rect(0.0, -1.0, 2.0, 1.0)
    d = segment_rect_distance(np.array(a, float), np.array(b, float), r)
    if sign_expected < 0:
        assert d < 0
    else:
        assert d == pytest.approx(sign_expected, abs=1e-9)


def test_segment_through_box_has_negative_penetration():
    r = Rect(0.0, 0.0, 10.0, 10.0)
    # segment well outside endpoints but crossing the whole box
    d = segment_rect_distance(np.array([-5.0, 5.0]), np.array([15.0, 5.0]), r)
    assert d < 0


def test_densify_respects_max_spacing_and_endpoints():
    P = np.array([[0.0, 0], [10.0, 0], [10.0, 10.0]])
    dense = densify(P, 1.0)
    seg_lens = np.hypot(*(dense[1:] - dense[:-1]).T)
    assert np.max(seg_lens) <= 1.0 + 1e-9
    assert np.allclose(dense[0], P[0])
    assert np.allclose(dense[-1], P[-1])
    # corner vertex (10,0) must be present
    assert any(np.allclose(q, [10.0, 0.0]) for q in dense)


def test_verify_detects_collision_between_control_points():
    # Two endpoints outside the box, straight segment crosses it. A
    # control-point-only check would wrongly accept this.
    r = [Rect(0.0, 0.0, 10.0, 10.0)]
    P = np.array([[-5.0, 5.0], [15.0, 5.0]])
    rep = verify_trajectory(P, r, clearance=0.0, max_spacing=0.5)
    assert rep["ok"] is False
    assert rep["min_segment_distance"] < 0


def test_collapse_and_expand_duplicates():
    P = np.array([[0.0, 0], [0.0, 0], [1.0, 0], [2.0, 0], [2.0, 0]])
    U, groups = collapse_consecutive_duplicates(P)
    assert len(U) == 3
    assert groups == [[0, 1], [2], [3, 4]]
    # non-consecutive revisits are preserved
    P2 = np.array([[0.0, 0], [1.0, 0], [0.0, 0]])
    U2, g2 = collapse_consecutive_duplicates(P2)
    assert len(U2) == 3
    back = expand_to_groups(U, groups)
    assert np.allclose(back, P)
