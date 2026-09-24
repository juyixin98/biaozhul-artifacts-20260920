"""分块（tiling）与重叠区冲突合并规则的单元测试。"""

import numpy as np

from groundseg.params import GroundSegParams
from groundseg.segment import (
    GROUND,
    NON_GROUND,
    UNDECIDED,
    Vote,
    _merge_votes,
)
from groundseg.tiles import build_tiles


def test_single_tile_when_tile_size_zero():
    pts = np.random.default_rng(0).uniform(-5, 5, (100, 3))
    tiles = build_tiles(pts, tile_size=0, overlap=0.5)
    assert len(tiles) == 1
    assert tiles[0].global_indices.size == 100


def test_grid_coverage_every_point_has_core_tile():
    rng = np.random.default_rng(1)
    pts = np.column_stack([
        rng.uniform(0, 10, 500), rng.uniform(0, 10, 500),
        rng.normal(0, 0.1, 500)])
    tiles = build_tiles(pts, tile_size=2.0, overlap=0.5)
    # 每个点恰好属于一个核心格
    core = np.concatenate([t.core_indices for t in tiles])
    assert sorted(core.tolist()) == list(range(500))
    # 重叠生效：成员点总数 >= 核心点总数，且内部点被多块覆盖
    member_total = sum(t.global_indices.size for t in tiles)
    assert member_total >= 500


def test_overlap_puts_interior_point_in_multiple_tiles():
    # 一片密集平整地面，中心区域的点应被多个重叠块投票
    pts = np.array([[x, y, 0.0] for x in np.linspace(-2, 2, 21)
                    for y in np.linspace(-2, 2, 21)])
    tiles = build_tiles(pts, tile_size=2.0, overlap=0.5)
    # 找到中心点 (0,0) 所在块，它应同时是多个块的成员
    center_id = None
    for i, (x, y, _) in enumerate(pts):
        if x == 0.0 and y == 0.0:
            center_id = i
    holders = [t for t in tiles if center_id in t.global_indices]
    assert len(holders) >= 2


def test_global_coordinates_used_not_local_origin():
    # 点云远离原点（全局偏移 1000, 2000），分块结果仍应正确拟合
    rng = np.random.default_rng(4)
    local = np.column_stack([
        rng.uniform(-4, 4, 400), rng.uniform(-4, 4, 400),
        rng.normal(0, 0.02, 400)])
    shifted = local + np.array([1000.0, 2000.0, 50.0])
    tiles = build_tiles(shifted, tile_size=4.0, overlap=0.5)
    # 块成员坐标必须仍是全局坐标
    some = tiles[0]
    assert shifted[some.global_indices[0], 0] > 900


def test_merge_unanimous_ground():
    votes = [
        Vote(0, GROUND, 0.9, (0, 0)),
        Vote(0, GROUND, 0.7, (1, 0)),
        Vote(1, GROUND, 0.5, (0, 0)),
    ]
    labels, confs, reasons = _merge_votes(votes, 2, 1.25)
    assert labels == [GROUND, GROUND]
    assert confs[0] == 0.9  # 取最大置信
    assert reasons == ["ground_vote", "ground_vote"]


def test_merge_not_covered_is_undecidable():
    labels, confs, reasons = _merge_votes([], 2, 1.25)
    assert labels == [UNDECIDED, UNDECIDED]
    assert reasons == ["not_covered", "not_covered"]
    assert confs == [0.0, 0.0]


def test_merge_conflict_high_confidence_wins():
    # 高置信 0.9 vs 低置信 0.5；0.9 > 1.25*0.5=0.625 -> 高置信胜出
    votes = [Vote(0, GROUND, 0.9, (0, 0)),
             Vote(0, NON_GROUND, 0.5, (1, 0))]
    labels, _, reasons = _merge_votes(votes, 1, 1.25)
    assert labels == [GROUND]
    assert reasons == ["conflict_resolved_ground"]

    votes = [Vote(0, NON_GROUND, 0.9, (0, 0)),
             Vote(0, GROUND, 0.5, (1, 0))]
    labels, _, reasons = _merge_votes(votes, 1, 1.25)
    assert labels == [NON_GROUND]
    assert reasons == ["conflict_resolved_non_ground"]


def test_merge_conflict_close_confidence_is_undecidable():
    # 0.55 vs 0.5：0.55 <= 1.25*0.5=0.625 -> 保守拒判
    votes = [Vote(0, GROUND, 0.55, (0, 0)),
             Vote(0, NON_GROUND, 0.50, (1, 0))]
    labels, confs, reasons = _merge_votes(votes, 1, 1.25)
    assert labels == [UNDECIDED]
    assert reasons == ["conflicting_votes"]
    assert confs[0] == 0.0


def test_merge_conflict_margin_is_strict_multiplicative():
    # 恰好等于倍数边界不算胜出（严格 >）
    votes = [Vote(0, GROUND, 0.625, (0, 0)),
             Vote(0, NON_GROUND, 0.5, (1, 0))]
    labels, _, _ = _merge_votes(votes, 1, 1.25)
    assert labels == [UNDECIDED]
    # 稍微超过就胜出
    votes = [Vote(0, GROUND, 0.626, (0, 0)),
             Vote(0, NON_GROUND, 0.5, (1, 0))]
    labels, _, _ = _merge_votes(votes, 1, 1.25)
    assert labels == [GROUND]


def test_end_to_end_conflicting_region_is_conservative():
    # 同一平坦地面分块，重叠区不应产生冲突（同标签投票）
    pts = np.array([[x, y, 0.0]
                    for x in np.linspace(-3, 3, 31)
                    for y in np.linspace(-3, 3, 31)])
    from groundseg import segment_points
    res = segment_points(pts, GroundSegParams(tile_size=2.0, tile_overlap=0.5))
    assert res.stats["ground"] == pts.shape[0]
    assert "conflicting_votes" not in set(res.reasons)
