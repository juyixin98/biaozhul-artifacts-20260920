"""Shared fixtures/helpers for the test suite."""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.geometry import Rect  # noqa: E402
from app.smoother import SmoothConfig  # noqa: E402


def dense_clearance(points, rects, spacing):
    """Independent reference collision check used by tests (not the service's)."""
    from app.geometry import densify, rect_signed_distance, segment_rect_distance
    pts = np.asarray(points, float)
    dense = densify(pts, spacing)
    m_sample = float("inf")
    for r in rects:
        m_sample = min(m_sample, float(np.min(rect_signed_distance(dense, r))))
    m_seg = float("inf")
    for a, b in zip(pts[:-1], pts[1:]):
        for r in rects:
            m_seg = min(m_seg, segment_rect_distance(a, b, r))
    if not rects:
        return float("inf")
    return min(m_sample, m_seg)


@pytest.fixture
def narrow_corridor():
    P = np.array([(0, 0), (2, 0.15), (4, -0.15), (6, 0.15),
                  (8, -0.15), (10, 0.15), (12, 0)], float)
    R = [Rect.from_center_wh(x, y, 4.0, 2.0)
         for x in (2, 6, 10) for y in (1.2, -1.2)]
    cfg = SmoothConfig(curvature_cap=0.8, corridor=0.45, clearance=0.05,
                       max_iterations=200)
    return P, R, cfg


@pytest.fixture
def corner_cut():
    P = np.array([(0, 0), (5, 0), (8, 0), (8, 5), (8, 10), (5, 10), (10, 10)], float)
    R = [Rect.from_center_wh(5, 5, 4, 4)]
    cfg = SmoothConfig(curvature_cap=0.8, corridor=1.5, clearance=0.05,
                       max_iterations=200)
    return P, R, cfg
