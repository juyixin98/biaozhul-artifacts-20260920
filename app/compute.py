"""真实的离线实验计算。

流水线 (全部确定性、纯 Python):
1. 读取 bag 摘要中的检测点 (机器人扫描到的障碍物点);
2. 应用坐标标定 (2x2 矩阵 + 平移), 过滤到参数给定的工作域;
3. 用运行种子驱动 SplitMix64, 按 sample_rate 对保留点做确定性抽样;
4. 在标定后的网格上做 4-邻域连通聚类 (并查集);
5. 输出每个簇的质心、点列表等结果, 浮点统一量化到 6 位小数。

相同输入 + 相同种子 -> 逐字节相同的规范化结果; 改变任意输入或种子 -> 不同结果。
"""

from __future__ import annotations

import json
from typing import Any

from .canonical import canonical_bytes
from .crypto import sha256_bytes
from .prng import SplitMix64

Q = 6  # 浮点量化位数


def parse_bag(raw: bytes) -> dict[str, Any]:
    try:
        bag = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"bag 不是合法 JSON: {exc}") from exc
    points = bag.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("bag.points 必须是非空数组")
    for i, p in enumerate(points):
        if (
            not isinstance(p, list)
            or len(p) != 2
            or not all(isinstance(c, (int, float)) for c in p)
        ):
            raise ValueError(f"bag.points[{i}] 必须是 [x, y] 数值对")
    if not isinstance(bag.get("bag_id"), str) or not bag["bag_id"]:
        raise ValueError("bag.bag_id 缺失")
    return bag


def summarize_bag(raw: bytes) -> dict[str, Any]:
    bag = parse_bag(raw)
    xs = [p[0] for p in bag["points"]]
    ys = [p[1] for p in bag["points"]]
    return {
        "bag_id": bag["bag_id"],
        "num_points": len(bag["points"]),
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "raw_sha256": sha256_bytes(raw),
    }


def _q(x: float) -> float:
    return round(float(x), Q)


def _calibrate(point: list[float], calib: dict[str, Any]) -> tuple[float, float]:
    (r00, r01), (r10, r11) = calib["matrix"]
    tx, ty = calib["translation"]
    x, y = point
    return r00 * x + r01 * y + tx, r10 * x + r11 * y + ty


def _grid_cluster(
    points: list[tuple[float, float]], grid_size: float
) -> list[list[int]]:
    """网格 4-邻域连通聚类, 返回每簇的点下标 (按处理顺序稳定)。"""
    cells: dict[tuple[int, int], int] = {}

    def cell_of(x: float, y: float) -> tuple[int, int]:
        import math

        return (math.floor(x / grid_size), math.floor(y / grid_size))

    parent = list(range(len(points)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, (x, y) in enumerate(points):
        cx, cy = cell_of(x, y)
        for dcx, dcy in ((0, 0), (-1, 0), (0, -1), (-1, -1), (1, -1)):
            j = cells.get((cx + dcx, cy + dcy))
            if j is not None:
                union(i, j)
        cells.setdefault((cx, cy), i)

    clusters: dict[int, list[int]] = {}
    for i in range(len(points)):
        clusters.setdefault(find(i), []).append(i)
    # 按下标最小值排序, 保证簇顺序稳定。
    return [clusters[k] for k in sorted(clusters, key=lambda r: min(clusters[r]))]


def run_experiment(
    bag_raw: bytes,
    params: dict[str, Any],
    calibration: dict[str, Any],
    algorithm: dict[str, Any],
    snapshot_id: str,
    bag_sha256: str,
    params_sha256: str,
    calibration_sha256: str,
    algorithm_sha256: str,
    seed: int,
) -> dict[str, Any]:
    bag = parse_bag(bag_raw)

    # 1. 标定 + 2. 工作域过滤
    xmin, ymin, xmax, ymax = params["bounds"]
    calibrated: list[tuple[float, float]] = []
    for p in bag["points"]:
        x, y = _calibrate(p, calibration)
        if xmin <= x <= xmax and ymin <= y <= ymax:
            calibrated.append((x, y))

    # 3. 种子驱动的确定性抽样
    rng = SplitMix64(seed)
    kept = [p for p in calibrated if rng.uniform() < params["sample_rate"]]

    # 4. 网格聚类
    groups = _grid_cluster(kept, params["grid_size"])

    # 5. 结果清单
    clusters = []
    for g in groups:
        if len(g) < params["min_cluster_size"]:
            continue
        cx = sum(kept[i][0] for i in g) / len(g)
        cy = sum(kept[i][1] for i in g) / len(g)
        clusters.append(
            {
                "size": len(g),
                "centroid": [_q(cx), _q(cy)],
                "points": [[_q(kept[i][0]), _q(kept[i][1])] for i in g],
            }
        )

    return {
        "schema": "robot-experiment-result/v1",
        "snapshot_id": snapshot_id,
        "inputs": {
            "bag_sha256": bag_sha256,
            "params_sha256": params_sha256,
            "calibration_sha256": calibration_sha256,
            "algorithm_sha256": algorithm_sha256,
        },
        "seed": seed,
        "algorithm": {"name": algorithm["name"], "version": algorithm["version"]},
        "stats": {
            "points_total": len(bag["points"]),
            "points_in_bounds": len(calibrated),
            "points_sampled": len(kept),
            "num_clusters": len(clusters),
        },
        "clusters": clusters,
    }


def result_bytes(result: dict[str, Any]) -> bytes:
    """产物的规范字节 (哈希与落盘都基于它)。"""
    return canonical_bytes(result)


def verify_result(result: dict[str, Any], expected: bytes) -> bool:
    """重算结果的规范字节必须与发布前记录的逐字节相等。"""
    return canonical_bytes(result) == expected
