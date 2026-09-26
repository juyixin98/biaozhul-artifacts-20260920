"""位姿图数据结构与连通性诊断。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .kernels import Kernel
from .se2 import compose_pose, invert_pose, pose_error, rotation_matrix, wrap_angle


@dataclass
class Node:
    """图节点（一个 SE2 位姿）。

    Attributes:
        id: 全局唯一节点编号。
        initial_pose: 初始/先验位姿 ``[x, y, theta]``。
        fixed: 是否固定（固定节点用于规范全局平移与旋转自由度）。
    """

    id: int
    initial_pose: np.ndarray
    fixed: bool = False

    def __post_init__(self) -> None:
        self.initial_pose = np.asarray(self.initial_pose, dtype=float).reshape(3).copy()
        self.initial_pose[2] = wrap_angle(self.initial_pose[2])


@dataclass
class Edge:
    """两个节点间的相对位姿约束。

    测量含义：``T_j ≈ T_i * Z``，``info`` 为 3x3 正定信息矩阵
    （协方差之逆）。
    """

    id: int
    i: int
    j: int
    measurement: np.ndarray
    info: np.ndarray
    kernel: Kernel = field(default_factory=lambda: Kernel("none"))

    def __post_init__(self) -> None:
        self.measurement = np.asarray(self.measurement, dtype=float).reshape(3).copy()
        self.measurement[2] = wrap_angle(self.measurement[2])
        info = np.asarray(self.info, dtype=float).reshape(3, 3)
        if not np.allclose(info, info.T, atol=1e-10):
            raise ValueError(f"边 {self.id}: 信息矩阵必须对称")
        self.info = info

    def residual(self, poses: dict[int, np.ndarray]) -> np.ndarray:
        return pose_error(poses[self.i], poses[self.j], self.measurement)

    def chi2(self, poses: dict[int, np.ndarray]) -> float:
        e = self.residual(poses)
        return float(e @ self.info @ e)


def connected_components(
    node_ids: list[int], edges: list[Edge]
) -> list[list[int]]:
    """并查集求无向连通分量，用于图不连通诊断。

    回环边与里程计边在此都视为无向连接（约束对两端都有耦合）。
    """
    parent = {nid: nid for nid in node_ids}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for edge in edges:
        union(edge.i, edge.j)

    groups: dict[int, list[int]] = {}
    for nid in node_ids:
        groups.setdefault(find(nid), []).append(nid)
    components = [sorted(g) for g in groups.values()]
    components.sort(key=lambda g: (len(g), g[0]), reverse=True)
    return components


class PoseGraph:
    """SE2 位姿图：节点、相对约束边与（可选）先验约束。"""

    def __init__(self) -> None:
        self.nodes: dict[int, Node] = {}
        self.edges: list[Edge] = []
        self._edge_seq = 0

    def add_node(
        self, node_id: int, pose: np.ndarray, fixed: bool = False
    ) -> Node:
        if node_id in self.nodes:
            raise ValueError(f"节点 {node_id} 已存在")
        node = Node(node_id, pose, fixed)
        self.nodes[node_id] = node
        return node

    def add_edge(
        self,
        i: int,
        j: int,
        measurement,
        info,
        kernel: Kernel | None = None,
        edge_id: int | None = None,
    ) -> Edge:
        if i not in self.nodes or j not in self.nodes:
            raise ValueError(f"边引用了不存在的节点: {i} -> {j}")
        if i == j:
            raise ValueError(f"自环边不支持: 节点 {i}")
        if edge_id is None:
            edge_id = self._edge_seq
            self._edge_seq += 1
        edge = Edge(
            edge_id, i, j, measurement, info, kernel or Kernel("none")
        )
        self.edges.append(edge)
        return edge

    @property
    def ordered_ids(self) -> list[int]:
        return sorted(self.nodes.keys())

    def fixed_node_count(self) -> int:
        return sum(1 for n in self.nodes.values() if n.fixed)

    def initial_poses(self) -> dict[int, np.ndarray]:
        return {nid: node.initial_pose.copy() for nid, node in self.nodes.items()}

    def check_positive_definite_info(self, tol: float = 1e-12) -> list[str]:
        """检查信息矩阵正定性，返回问题描述列表（空列表表示全部通过）。"""
        problems: list[str] = []
        for edge in self.edges:
            eigvals = np.linalg.eigvalsh(edge.info)
            if eigvals.min() < -tol:
                problems.append(f"边 {edge.id} ({edge.i}->{edge.j}): 信息矩阵非正定")
        return problems

    def predicted_measurement(
        self, poses: dict[int, np.ndarray], i: int, j: int
    ) -> np.ndarray:
        """给定位姿，计算 i->j 的真实相对变换（合成数据/校验用）。"""
        rel = compose_pose(invert_pose(poses[i]), poses[j])
        return rel


def make_information_matrix(
    sigma_xy: float, sigma_theta: float, corr: float = 0.0
) -> np.ndarray:
    """由平移/角度标准差构造常用对角（或带相关系数的）信息矩阵。

    合成数据生成时的便捷工具：``Ω = diag(1/σx², 1/σy², 1/σθ²)``。
    """
    if sigma_xy <= 0.0 or sigma_theta <= 0.0:
        raise ValueError("标准差必须为正数")
    if not -1.0 < corr < 1.0:
        raise ValueError("相关系数 corr 必须在 (-1, 1) 内")
    sigma = np.array([sigma_xy, sigma_xy, sigma_theta])
    corr_mat = np.array(
        [
            [1.0, corr, 0.0],
            [corr, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    cov = (sigma[:, None] @ sigma[None, :]) * corr_mat
    return np.linalg.inv(cov)


def transform_local_to_global(
    pose_i: np.ndarray, local_xy: np.ndarray
) -> np.ndarray:
    """把 i 坐标系下的二维点变换到全局坐标系（合成轨迹辅助函数）。"""
    return pose_i[:2] + rotation_matrix(pose_i[2]) @ np.asarray(
        local_xy, dtype=float
    )
