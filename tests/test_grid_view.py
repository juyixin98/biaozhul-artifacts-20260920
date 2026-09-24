"""栅格代价视图与斜穿规则测试。"""

import math

import numpy as np
import pytest

from app.planner import CONNECTIVITY_4, CONNECTIVITY_8, GridView


def make(weights=None, blocked=None, conn=8, shape=(4, 4)):
    w = np.ones(shape) if weights is None else np.asarray(weights, dtype=np.float64)
    b = np.zeros(shape, dtype=bool) if blocked is None else np.asarray(blocked, dtype=bool)
    return GridView(w, b, conn)


def test_straight_edge_cost_is_mean():
    v = make()
    # 直边 = 两端权重均值
    assert v.edge_cost((0, 0), (0, 1)) == pytest.approx(1.0)


def test_diagonal_edge_cost():
    v = make()
    assert v.edge_cost((0, 0), (1, 1)) == pytest.approx(math.sqrt(2.0))


def test_weighted_edge_cost():
    w = np.ones((3, 3))
    w[0, 0] = 3.0
    w[0, 1] = 5.0
    v = make(weights=w, shape=(3, 3))
    assert v.edge_cost((0, 0), (0, 1)) == pytest.approx(4.0)
    assert v.edge_cost((0, 0), (1, 1)) == pytest.approx(2.0 * math.sqrt(2.0))


def test_blocked_endpoint_has_no_edge():
    b = np.zeros((3, 3), dtype=bool)
    b[0, 1] = True
    v = make(blocked=b, shape=(3, 3))
    assert v.edge_cost((0, 0), (0, 1)) is None
    assert v.edge_cost((0, 1), (0, 0)) is None


def test_diagonal_blocked_when_both_corners_blocked():
    # 斜穿两障碍夹角禁止: (0,1) 与 (1,0) 都堵 => (0,0)->(1,1) 禁止
    b = np.zeros((3, 3), dtype=bool)
    b[0, 1] = b[1, 0] = True
    v = make(blocked=b, shape=(3, 3))
    assert v.edge_cost((0, 0), (1, 1)) is None


def test_diagonal_allowed_along_single_wall():
    # 只有一侧堵: 贴墙斜行允许
    b = np.zeros((3, 3), dtype=bool)
    b[1, 0] = True
    v = make(blocked=b, shape=(3, 3))
    assert v.edge_cost((0, 0), (1, 1)) is not None


def test_connectivity4_has_no_diagonal():
    v = make(conn=CONNECTIVITY_4)
    assert v.edge_cost((0, 0), (1, 1)) is None


def test_connectivity8_has_diagonal():
    v = make(conn=CONNECTIVITY_8)
    assert v.edge_cost((0, 0), (1, 1)) is not None


def test_non_adjacent_is_none():
    v = make()
    assert v.edge_cost((0, 0), (2, 2)) is None
    assert v.edge_cost((0, 0), (0, 5)) is None


def test_zero_weight_edges_are_zero():
    w = np.zeros((3, 3))
    v = make(weights=w, shape=(3, 3))
    assert v.edge_cost((0, 0), (0, 1)) == 0.0
    assert v.edge_cost((0, 0), (1, 1)) == 0.0
