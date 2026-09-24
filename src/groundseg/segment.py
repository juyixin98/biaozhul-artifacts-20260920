"""分块 RANSAC 地面分割主流程。

标签语义（三值，保守原则）：
    "ground"      地面点
    "non_ground"  可靠判定的非地面点（离可信地面平面足够远）
    "undecidable" 不可判定（数据不足/平面不可靠/重叠块投票冲突）

关键原则：**最大的平面不会自动被当作地面**。一个平面必须同时通过
倾角门槛与内点比例（含绝对数量）门槛，才被承认为地面；否则整块
返回 undecidable，而不是退而求其次把“最大平面”标成地面。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .params import GroundSegParams
from .planes import Plane, ransac_plane
from .tiles import Tile, build_tiles

GROUND = "ground"
NON_GROUND = "non_ground"
UNDECIDED = "undecidable"


@dataclass(frozen=True)
class Vote:
    """单个块对单个全局点的一次分类投票。"""
    global_index: int
    label: str
    confidence: float
    tile: tuple[int, int]


@dataclass(frozen=True)
class BlockResult:
    tile: tuple[int, int]
    n_points: int
    n_core_points: int
    n_unique: int
    decided: bool
    label: str | None            # 块层面只可能是 "ground" 或 None
    reason: str
    inlier_ratio: float
    inlier_count: int
    tilt_deg: float | None
    plane_normal: tuple[float, float, float] | None
    plane_offset: float | None
    confidence: float           # 块级置信度（通过门槛后 (0,1]，否则 0）


@dataclass(frozen=True)
class SegmentResult:
    labels: list[str]
    confidences: list[float]
    reasons: list[str]
    blocks: list[BlockResult] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def _classify_tile(
    points: np.ndarray,
    tile: Tile,
    p: GroundSegParams,
    seed_rng: np.random.Generator,
    local_seed_indices: np.ndarray | None,
) -> tuple[BlockResult, list[Vote]]:
    """处理单个块，返回块结论与点级投票。

    所有坐标都是全局坐标；global_indices 负责把局部结论映射回全局 ID。
    """
    gids = tile.global_indices
    local = points[gids]
    n = local.shape[0]

    def reject(reason: str) -> tuple[BlockResult, list[Vote]]:
        br = BlockResult(
            tile=tile.index, n_points=n, n_core_points=tile.core_indices.size,
            n_unique=0, decided=False, label=None, reason=reason,
            inlier_ratio=0.0, inlier_count=0, tilt_deg=None,
            plane_normal=None, plane_offset=None, confidence=0.0,
        )
        return br, []

    # 数值去重（按坐标尺度自适应容差），重复点不参与规模门槛与采样。
    spread = float(max(np.ptp(local, axis=0).max(), 1e-9))
    dedup_tol = 1e-8 * spread
    keys = np.round(local / dedup_tol).astype(np.int64)
    _, unique_inverse, unique_counts = np.unique(
        keys, axis=0, return_inverse=True, return_counts=True
    )
    n_unique = int(unique_counts.size)

    if n < 3:
        return reject("too_few_points")
    if n_unique < p.min_unique_points:
        br = BlockResult(
            tile=tile.index, n_points=n, n_core_points=tile.core_indices.size,
            n_unique=n_unique, decided=False, label=None,
            reason="insufficient_unique_points",
            inlier_ratio=0.0, inlier_count=0, tilt_deg=None,
            plane_normal=None, plane_offset=None, confidence=0.0,
        )
        return br, []

    rr = ransac_plane(
        local,
        iterations=p.ransac_iterations,
        distance_threshold=p.distance_threshold,
        rng=seed_rng,
        seed_indices=local_seed_indices,
        unique_scale=dedup_tol,
    )
    if rr.plane is None:
        return reject(f"ransac_{rr.reason}")

    plane: Plane = rr.plane
    tilt = plane.tilt_deg()
    inlier_count = int(rr.inlier_mask.sum())

    # —— 地面可靠性硬门槛：倾角 + 内点比例 + 内点绝对数量 ——
    if tilt > p.max_tilt_deg:
        br = BlockResult(
            tile=tile.index, n_points=n, n_core_points=tile.core_indices.size,
            n_unique=n_unique, decided=False, label=None,
            reason="tilt_exceeds_max",
            inlier_ratio=rr.inlier_ratio, inlier_count=inlier_count,
            tilt_deg=tilt, plane_normal=tuple(float(v) for v in plane.normal),
            plane_offset=plane.offset, confidence=0.0,
        )
        return br, []

    if rr.inlier_ratio < p.min_inlier_ratio or inlier_count < p.min_inlier_count:
        br = BlockResult(
            tile=tile.index, n_points=n, n_core_points=tile.core_indices.size,
            n_unique=n_unique, decided=False, label=None,
            reason="inlier_ratio_too_low",
            inlier_ratio=rr.inlier_ratio, inlier_count=inlier_count,
            tilt_deg=tilt, plane_normal=tuple(float(v) for v in plane.normal),
            plane_offset=plane.offset, confidence=0.0,
        )
        return br, []

    # —— 平面被承认为可信地面平面：逐点投票 ——
    angle_margin = float(np.clip(1.0 - tilt / p.max_tilt_deg, 0.05, 1.0))
    ratio_head = max(rr.inlier_ratio - p.min_inlier_ratio, 0.0)
    ratio_margin = float(
        np.clip(ratio_head / max(1.0 - p.min_inlier_ratio, 1e-9), 0.05, 1.0)
    )
    block_conf = float(np.clip(angle_margin * ratio_margin, 0.0, 1.0))

    dist = np.abs(plane.signed_distance(local))
    votes: list[Vote] = []
    near = dist <= p.point_margin * p.distance_threshold
    for li in range(n):
        d = float(dist[li])
        if near[li]:
            # 越贴近平面置信越高：d=0 -> 1，d=margin*阈值 -> 0.05
            d_margin = float(
                np.clip(1.0 - d / (p.point_margin * p.distance_threshold), 0.05, 1.0)
            )
            conf = block_conf * (0.5 + 0.5 * d_margin)
            votes.append(Vote(int(gids[li]), GROUND, float(np.clip(conf, 0.0, 1.0)), tile.index))
        else:
            # 明显在平面上方/下方的点：偏离越远非地面置信越高
            far_margin = float(
                np.clip(
                    (d / (p.point_margin * p.distance_threshold) - 1.0) / 2.0,
                    0.05, 1.0,
                )
            )
            conf = block_conf * (0.5 + 0.5 * far_margin)
            votes.append(Vote(int(gids[li]), NON_GROUND, float(np.clip(conf, 0.0, 1.0)), tile.index))

    br = BlockResult(
        tile=tile.index, n_points=n, n_core_points=tile.core_indices.size,
        n_unique=n_unique, decided=True, label=GROUND, reason="ground_plane",
        inlier_ratio=rr.inlier_ratio, inlier_count=inlier_count,
        tilt_deg=tilt, plane_normal=tuple(float(v) for v in plane.normal),
        plane_offset=plane.offset, confidence=block_conf,
    )
    return br, votes


def _merge_votes(
    votes: list[Vote],
    n_points: int,
    conflict_margin: float,
) -> tuple[list[str], list[float], list[str]]:
    """按明确的置信规则合并（可能来自多个重叠块的）投票。

    规则：
      1. 没有任何块投票 -> undecidable / "not_covered"。
      2. 所有投票同标签 -> 取该标签，置信度取最大值。
      3. 标签冲突 -> 比较两侧最高置信度：
         max_conf(高) > conflict_margin * max_conf(低) 才采纳高置信一侧；
         否则保守判 undecidable / "conflicting_votes"。
    """
    best_ground = np.full(n_points, -1.0, dtype=np.float64)
    best_non = np.full(n_points, -1.0, dtype=np.float64)
    for v in votes:
        if v.label == GROUND:
            if v.confidence > best_ground[v.global_index]:
                best_ground[v.global_index] = v.confidence
        elif v.label == NON_GROUND:
            if v.confidence > best_non[v.global_index]:
                best_non[v.global_index] = v.confidence

    labels: list[str] = []
    confs: list[float] = []
    reasons: list[str] = []
    for i in range(n_points):
        g, ng = best_ground[i], best_non[i]
        has_g, has_ng = g >= 0.0, ng >= 0.0
        if not has_g and not has_ng:
            labels.append(UNDECIDED)
            confs.append(0.0)
            reasons.append("not_covered")
        elif has_g and not has_ng:
            labels.append(GROUND)
            confs.append(float(g))
            reasons.append("ground_vote")
        elif has_ng and not has_g:
            labels.append(NON_GROUND)
            confs.append(float(ng))
            reasons.append("non_ground_vote")
        else:
            # 重叠区冲突：必须有一侧严格按倍数胜出。
            hi, lo = max(g, ng), min(g, ng)
            if hi > conflict_margin * lo:
                if g > ng:
                    labels.append(GROUND)
                    confs.append(float(g))
                    reasons.append("conflict_resolved_ground")
                else:
                    labels.append(NON_GROUND)
                    confs.append(float(ng))
                    reasons.append("conflict_resolved_non_ground")
            else:
                labels.append(UNDECIDED)
                confs.append(0.0)
                reasons.append("conflicting_votes")
    return labels, confs, reasons


def segment_points(
    points: np.ndarray | list,
    params: GroundSegParams | None = None,
    *,
    seed_indices: list[int] | np.ndarray | None = None,
) -> SegmentResult:
    """对点云执行分块 RANSAC 地面分割。

    参数
    ----
    points: (N, 3) 数组，全局坐标。
    params: GroundSegParams，缺省用默认参数。
    seed_indices: 调用方先验地面种子的**全局下标**（可选）。

    返回 SegmentResult，长度均为 N，与输入顺序一一对应。
    """
    p = params or GroundSegParams()
    p.validate()

    pts = np.asarray(points, dtype=np.float64)
    if pts.size == 0:
        pts = pts.reshape(0, 3)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError("points 必须是 (N, 3) 数组")
    n = pts.shape[0]
    if n == 0:
        return SegmentResult(
            labels=[], confidences=[], reasons=[], blocks=[],
            stats={"ground": 0, "non_ground": 0, "undecidable": 0, "total": 0},
        )

    seed_arr: np.ndarray | None = None
    if seed_indices is not None:
        seed_arr = np.asarray(seed_indices, dtype=np.int64)
        if seed_arr.size > 0 and (seed_arr.min() < 0 or seed_arr.max() >= n):
            raise ValueError("seed_indices 含越界下标")
    elif p.seed_indices is not None:
        cand = np.asarray(p.seed_indices, dtype=np.int64)
        seed_arr = cand[(cand >= 0) & (cand < n)]

    tiles: list[Tile] = build_tiles(
        pts, tile_size=p.tile_size, overlap=p.tile_overlap
    )

    # 每个块使用独立但可复现的派生随机流：全局种子 + 块网格坐标 + 序号。
    # 这样块的处理顺序变化（例如裁剪点云导致网格平移除外）不会互相串扰，
    # 同一输入 + 同一参数总是逐点复现。
    all_votes: list[Vote] = []
    block_results: list[BlockResult] = []
    for k, tile in enumerate(tiles):
        child_rng = np.random.default_rng(
            np.random.SeedSequence(
                p.seed_rng,
                spawn_key=(tile.index[0], tile.index[1], k),
            )
        )
        local_seeds: np.ndarray | None = None
        if seed_arr is not None and seed_arr.size >= 3:
            # 全局种子下标 -> 块内局部下标
            gid_to_local = np.full(n, -1, dtype=np.int64)
            gid_to_local[tile.global_indices] = np.arange(tile.global_indices.size)
            mapped = gid_to_local[seed_arr]
            mapped = mapped[mapped >= 0]
            if mapped.size >= 3:
                local_seeds = mapped

        br, votes = _classify_tile(pts, tile, p, child_rng, local_seeds)
        block_results.append(br)
        all_votes.extend(votes)

    labels, confidences, reasons = _merge_votes(
        all_votes, n, p.conflict_margin
    )

    stats = {
        "total": n,
        "ground": labels.count(GROUND),
        "non_ground": labels.count(NON_GROUND),
        "undecidable": labels.count(UNDECIDED),
        "blocks": len(tiles),
        "decided_blocks": sum(1 for b in block_results if b.decided),
    }
    return SegmentResult(
        labels=labels,
        confidences=confidences,
        reasons=reasons,
        blocks=block_results,
        stats=stats,
    )
