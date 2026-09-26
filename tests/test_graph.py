"""位姿图结构：节点/边校验、连通分量诊断、信息矩阵。"""

from __future__ import annotations

import numpy as np
import pytest

from pose_graph_optimizer.graph import (
    PoseGraph,
    connected_components,
    make_information_matrix,
)
from pose_graph_optimizer.kernels import Kernel


def _minimal_graph(n: int = 3) -> PoseGraph:
    g = PoseGraph()
    info = np.eye(3)
    for k in range(n):
        g.add_node(k, [0.0, 0.0, 0.0], fixed=(k == 0))
    for k in range(n - 1):
        g.add_edge(k, k + 1, [1.0, 0.0, 0.0], info)
    return g


def test_duplicate_node_rejected():
    g = PoseGraph()
    g.add_node(0, [0, 0, 0])
    with pytest.raises(ValueError):
        g.add_node(0, [1, 1, 0])


def test_edge_references_must_exist_and_no_self_loop():
    g = PoseGraph()
    g.add_node(0, [0, 0, 0])
    with pytest.raises(ValueError):
        g.add_edge(0, 9, [1, 0, 0], np.eye(3))
    with pytest.raises(ValueError):
        g.add_edge(0, 0, [1, 0, 0], np.eye(3))


def test_nonsymmetric_info_rejected():
    g = PoseGraph()
    g.add_node(0, [0, 0, 0])
    g.add_node(1, [1, 0, 0])
    bad = np.array([[1.0, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    with pytest.raises(ValueError):
        g.add_edge(0, 1, [1, 0, 0], bad)


def test_connected_components_two_chains():
    g = _minimal_graph(4)  # 0-1-2-3
    for k in range(3):
        g.add_node(10 + k, [5.0, 5.0, 0.0])
    g.add_edge(10, 11, [1, 0, 0], np.eye(3))
    g.add_edge(11, 12, [1, 0, 0], np.eye(3))

    comps = connected_components(g.ordered_ids, g.edges)
    assert len(comps) == 2
    assert comps[0] == [0, 1, 2, 3]          # 主分量排在最前
    assert comps[1] == [10, 11, 12]


def test_loop_edge_makes_one_component():
    # 0-1-2-3 加一条 3->0 回环，仍然是单一连通分量
    g = _minimal_graph(4)
    g.add_edge(3, 0, [0, 0, 0], np.eye(3), Kernel("none"))
    comps = connected_components(g.ordered_ids, g.edges)
    assert comps == [[0, 1, 2, 3]]


def test_information_matrix_from_sigmas_is_inverse_of_diagonal_cov():
    info = make_information_matrix(0.1, 0.05)
    cov = np.linalg.inv(info)
    assert np.allclose(np.diag(cov), [0.01, 0.01, 0.0025])
    assert np.allclose(cov - np.diag(np.diag(cov)), 0.0)
    with pytest.raises(ValueError):
        make_information_matrix(-1.0, 0.1)
    with pytest.raises(ValueError):
        make_information_matrix(0.1, 0.1, corr=1.0)


def test_edge_residual_and_predicted_measurement():
    from pose_graph_optimizer.se2 import compose_pose

    g = _minimal_graph(2)
    poses = {0: np.array([0.0, 0.0, 0.0]), 1: np.array([1.0, 0.0, 0.0])}
    edge = g.edges[0]
    assert np.allclose(edge.residual(poses), 0.0, atol=1e-12)
    assert edge.chi2(poses) == pytest.approx(0.0, abs=1e-12)
    pred = g.predicted_measurement(poses, 0, 1)
    assert np.allclose(pred, [1.0, 0.0, 0.0])
    # 平移一个节点后残差非零
    poses[1] = np.array([1.1, 0.0, 0.0])
    assert edge.chi2(poses) > 0.0
    assert compose_pose(poses[0], pred)[0] == pytest.approx(1.0)


def test_local_to_global_transform():
    from pose_graph_optimizer.graph import transform_local_to_global

    pose = np.array([1.0, 2.0, np.pi / 2])
    # 局部 x 方向旋转后指向全局 +y
    pt = transform_local_to_global(pose, np.array([1.0, 0.0]))
    assert np.allclose(pt, [1.0, 3.0], atol=1e-12)
