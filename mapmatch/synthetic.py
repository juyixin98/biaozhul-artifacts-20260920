"""合成道路图与轨迹场景（无真实硬件，全部离线生成）。"""

from __future__ import annotations

import numpy as np

from .graph import Node, Edge, RoadGraph


def _bidirectional_edges(prefix: str, pts: list[tuple[float, float]]) -> list[Edge]:
    """按折线点序列生成双向边。"""
    edges = []
    for i in range(len(pts) - 1):
        u, v = f"{prefix}N{i}", f"{prefix}N{i+1}"
        edges.append(Edge(f"{prefix}E{i}_f", u, v, (pts[i], pts[i + 1])))
        edges.append(Edge(f"{prefix}E{i}_r", v, u, (pts[i + 1], pts[i])))
    return edges


def _line_points(x0, y0, x1, y1, step) -> list[tuple[float, float]]:
    n = max(2, int(round(np.hypot(x1 - x0, y1 - y0) / step)) + 1)
    return list(zip(np.linspace(x0, x1, n), np.linspace(y0, y1, n)))


# --------------------------------------------------------------------------
# 场景 1：平行道路（两条东西向道路相距 20m，两端由连接道相连）
# --------------------------------------------------------------------------
def make_parallel_roads_graph() -> RoadGraph:
    nodes: list[Node] = []
    edges: list[Edge] = []
    a_pts = _line_points(0, 0, 1000, 0, 100)
    b_pts = _line_points(0, 20, 1000, 20, 100)
    for i, pt in enumerate(a_pts):
        nodes.append(Node(f"AN{i}", pt[0], pt[1]))
    for i, pt in enumerate(b_pts):
        nodes.append(Node(f"BN{i}", pt[0], pt[1]))
    for i in range(len(a_pts) - 1):
        edges.append(Edge(f"AE{i}_f", f"AN{i}", f"AN{i+1}", (a_pts[i], a_pts[i + 1])))
        edges.append(Edge(f"AE{i}_r", f"AN{i+1}", f"AN{i}", (a_pts[i + 1], a_pts[i])))
    for i in range(len(b_pts) - 1):
        edges.append(Edge(f"BE{i}_f", f"BN{i}", f"BN{i+1}", (b_pts[i], b_pts[i + 1])))
        edges.append(Edge(f"BE{i}_r", f"BN{i+1}", f"BN{i}", (b_pts[i + 1], b_pts[i])))
    # 两端连接道（双向）
    for tag, pa, pb in [("w", a_pts[0], b_pts[0]), ("e", a_pts[-1], b_pts[-1])]:
        ia, ib = (0, 0) if tag == "w" else (len(a_pts) - 1, len(b_pts) - 1)
        edges.append(Edge(f"CW_{tag}_n", f"AN{ia}", f"BN{ib}", (pa, pb)))
        edges.append(Edge(f"CW_{tag}_s", f"BN{ib}", f"AN{ia}", (pb, pa)))
    return RoadGraph(nodes, edges)


def make_parallel_trajectory(rng: np.random.Generator | None = None, sigma: float = 6.0):
    """沿南侧道路 A（y=0）由西向东行驶，叠加高斯噪声。返回 (ts, xs, ys, 真实边id序列)。"""
    rng = rng or np.random.default_rng(42)
    xs = np.arange(50.0, 951.0, 10.0)
    ts = np.arange(len(xs), dtype=float)
    ys = rng.normal(0.0, sigma, size=len(xs))
    xs = xs + rng.normal(0.0, sigma * 0.5, size=len(xs))
    true_edges = [f"AE{min(int(x // 100), 8)}_f" for x in np.arange(50.0, 951.0, 10.0)]
    return ts.tolist(), xs.tolist(), ys.tolist(), true_edges


# --------------------------------------------------------------------------
# 场景 2：立交无连接——水平路与垂直路几何相交于 (0,0)，但节点不连通
# --------------------------------------------------------------------------
def make_overpass_graph() -> RoadGraph:
    nodes: list[Node] = []
    edges: list[Edge] = []
    h_pts = _line_points(-500, 0, 500, 0, 100)
    v_pts = _line_points(0, -500, 0, 500, 100)
    for i, pt in enumerate(h_pts):
        nodes.append(Node(f"HN{i}", pt[0], pt[1]))
    for i, pt in enumerate(v_pts):
        nodes.append(Node(f"VN{i}", pt[0], pt[1]))
    for i in range(len(h_pts) - 1):
        edges.append(Edge(f"HE{i}_f", f"HN{i}", f"HN{i+1}", (h_pts[i], h_pts[i + 1])))
        edges.append(Edge(f"HE{i}_r", f"HN{i+1}", f"HN{i}", (h_pts[i + 1], h_pts[i])))
    for i in range(len(v_pts) - 1):
        edges.append(Edge(f"VE{i}_f", f"VN{i}", f"VN{i+1}", (v_pts[i], v_pts[i + 1])))
        edges.append(Edge(f"VE{i}_r", f"VN{i+1}", f"VN{i}", (v_pts[i + 1], v_pts[i])))
    # 注意：HN5 与 VN5 坐标都是 (0,0)，但它们是不同节点，二者之间没有任何边
    return RoadGraph(nodes, edges)


def make_overpass_trajectory(rng: np.random.Generator | None = None, sigma: float = 8.0):
    """沿水平路由西向东穿过立交点。"""
    rng = rng or np.random.default_rng(7)
    xs0 = np.arange(-450.0, 451.0, 10.0)
    ts = np.arange(len(xs0), dtype=float)
    xs = xs0 + rng.normal(0.0, sigma * 0.5, size=len(xs0))
    ys = rng.normal(0.0, sigma, size=len(xs0))
    true_edges = [f"HE{min(max(int((x + 500) // 100), 0), 8)}_f" for x in xs0]
    return ts.tolist(), xs.tolist(), ys.tolist(), true_edges


# --------------------------------------------------------------------------
# 场景 3：时间断档
# --------------------------------------------------------------------------
def make_gap_graph() -> RoadGraph:
    pts = _line_points(0, 0, 1000, 0, 100)
    nodes = [Node(f"GN{i}", x, y) for i, (x, y) in enumerate(pts)]
    edges = _bidirectional_edges("G", pts)
    return RoadGraph(nodes, edges)


def make_gap_trajectory():
    """两段行驶之间有 60s 断档。"""
    ts, xs, ys = [], [], []
    x = 100.0
    for k in range(10):
        ts.append(float(k))
        xs.append(x)
        ys.append(0.0)
        x += 10.0
    base = 9.0
    x = 500.0
    for k in range(10):
        ts.append(base + 60.0 + float(k))
        xs.append(x)
        ys.append(0.0)
        x += 10.0
    return ts, xs, ys


def make_far_outlier_trajectory():
    """轨迹中间夹一个远离所有道路的点（不可达/无候选）。"""
    ts = list(range(5))
    xs = [100.0, 110.0, 3000.0, 130.0, 140.0]
    ys = [0.0, 0.0, 2000.0, 0.0, 0.0]
    return ts, xs, ys
