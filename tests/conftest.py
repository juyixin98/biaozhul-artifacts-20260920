"""pytest 共享夹具。"""

from __future__ import annotations

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

G = 9.80665


@pytest.fixture
def rng():
    return np.random.default_rng(123)


@pytest.fixture
def static_payload(rng):
    """静止 30s + 中间 10s 剧烈运动的标准载荷。"""
    fs = 100.0
    t = np.arange(0, 40, 1 / fs)
    b = np.array([0.01, -0.02, 0.005])
    g = np.tile(b, (len(t), 1)) + rng.normal(0, 0.002, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.01, (len(t), 3))
    m = (t >= 15) & (t < 25)
    g[m] += rng.normal(0, 0.5, (m.sum(), 3))
    a[m] += rng.normal(0, 3.0, (m.sum(), 3))
    return {
        "timestamps": t.tolist(),
        "accel": a.tolist(),
        "gyro": g.tolist(),
    }
