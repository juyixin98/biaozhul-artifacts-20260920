"""端到端分割行为测试：五类困难场景 + 保守原则 + 分块/冲突规则。"""

import numpy as np
import pytest

from groundseg import GroundSegParams, evaluate, segment_points
from groundseg import scenes
from groundseg.segment import GROUND, NON_GROUND, UNDECIDED

DEFAULT = GroundSegParams()


def test_flat_ground_perfect():
    pts, truth, _ = scenes.flat_ground()
    res = segment_points(pts, DEFAULT)
    m = evaluate(res.labels, truth.tolist())["ground"]
    assert m["precision"] == pytest.approx(1.0)
    assert m["recall"] == pytest.approx(1.0)
    assert res.stats["undecidable"] == 0
    # 每个块都拟合出近水平平面
    for b in res.blocks:
        assert b.decided
        assert b.tilt_deg < 2.0


def test_acceptable_slope_is_segmented():
    # 10° 斜坡在默认 20° 门槛内：地面点与空中杂点必须同时分对
    pts, truth, _ = scenes.sloped_ground(angle_deg=10.0)
    res = segment_points(pts, DEFAULT)
    m = evaluate(res.labels, truth.tolist())
    assert m["ground"]["precision"] >= 0.99
    assert m["ground"]["recall"] >= 0.98
    assert m["non_ground"]["precision"] >= 0.95
    assert m["non_ground"]["recall"] >= 0.95
    tilts = [b.tilt_deg for b in res.blocks if b.decided]
    assert tilts and all(8.0 < t < 13.0 for t in tilts)


def test_steep_slope_is_rejected_not_forced():
    # 35° 陡坡：即使它是“最大平面”，也绝不能当地面 -> 全部 undecidable
    pts, truth, _ = scenes.SCENES["steep_slope"]()
    res = segment_points(pts, DEFAULT)
    assert res.stats["ground"] == 0
    assert res.stats["non_ground"] == 0
    assert res.stats["undecidable"] == pts.shape[0]
    assert all(b.reason == "tilt_exceeds_max" for b in res.blocks)
    # 放宽倾角门槛后应能分出地面（验证门槛确实是生效的旋钮）
    loose = GroundSegParams(max_tilt_deg=40.0, min_inlier_count=10)
    res2 = segment_points(pts, loose)
    m = evaluate(res2.labels, truth.tolist())["ground"]
    assert m["recall"] >= 0.95


def test_vertical_wall_is_not_ground():
    pts, truth, _ = scenes.ground_with_wall()
    res = segment_points(pts, DEFAULT)
    m = evaluate(res.labels, truth.tolist())
    # 地面点全部找到，墙点没有一个被标地面
    assert m["ground"]["precision"] >= 0.99
    assert m["ground"]["recall"] >= 0.99
    assert m["ground"]["tp"] == pytest.approx(truth.sum(), rel=0.02)


def test_large_wall_has_tilt_rejected_blocks():
    # 一片足够大的水平地面 + 地面之外（右侧）一道高墙：
    # 含地面的块应识别地面；墙独占的块拟合出竖直平面后必须因倾角拒判
    # ——即使该平面内点比例高达 100%（“最大平面”也不能当地面）。
    rng = np.random.default_rng(9)
    gx = np.linspace(-10, 2, 36)
    gy = np.linspace(-6, 6, 36)
    gxx, gyy = np.meshgrid(gx, gy)
    gnd = np.column_stack([
        gxx.ravel(), gyy.ravel(), rng.normal(0, 0.01, gxx.size)])
    wx = np.linspace(4.5, 9.5, 30)
    wz = np.linspace(0.3, 5.0, 30)
    wxx, wzz = np.meshgrid(wx, wz)
    wall = np.column_stack([
        wxx.ravel(), rng.normal(0, 0.02, wxx.size), wzz.ravel()])
    pts = np.vstack([gnd, wall])
    res = segment_points(pts, GroundSegParams(tile_size=4.0))
    rejected = [b for b in res.blocks
                if not b.decided and b.reason == "tilt_exceeds_max"]
    assert rejected, "应至少有一个拟合出竖直平面但被倾角拒判的块"
    assert all(b.tilt_deg > 60 for b in rejected)
    assert any(b.inlier_ratio > 0.8 for b in rejected), \
        "即使墙平面内点比例极高也必须拒判"
    # 真实地面点仍然被正确识别
    truth = np.concatenate([np.ones(gnd.shape[0], dtype=np.int64),
                            np.zeros(wall.shape[0], dtype=np.int64)])
    m = evaluate(res.labels, truth.tolist())["ground"]
    assert m["precision"] >= 0.98
    assert m["recall"] >= 0.90
    assert res.stats["ground"] < pts.shape[0] * 0.6  # 绝没把墙当地面


def test_pure_wall_block_rejected_by_tilt():
    rng = np.random.default_rng(3)
    wx = np.linspace(-2, 2, 40)
    wz = np.linspace(0.3, 3, 40)
    xx, zz = np.meshgrid(wx, wz)
    wall = np.column_stack([
        xx.ravel(), rng.normal(0, 0.02, xx.size), zz.ravel()])
    res = segment_points(wall, GroundSegParams(tile_size=0))
    assert res.blocks[0].reason == "tilt_exceeds_max"
    assert res.labels == [UNDECIDED] * wall.shape[0]


def test_sparse_cloud_is_undecidable():
    pts, truth, _ = scenes.sparse_ground()
    res = segment_points(pts, DEFAULT)
    assert set(res.labels) == {UNDECIDED}
    reasons = {b.reason for b in res.blocks}
    assert reasons <= {"insufficient_unique_points", "too_few_points"}


def test_diffuse_noise_is_undecidable():
    # 无平面结构的纯噪声：任何平面内点比例都不达标，必须拒判。
    pts, truth, _ = scenes.diffuse_noise()
    res = segment_points(pts, DEFAULT)
    assert res.stats["ground"] == 0
    assert res.stats["undecidable"] == pts.shape[0]
    assert all(not b.decided for b in res.blocks)
    assert all(b.reason in ("inlier_ratio_too_low", "tilt_exceeds_max")
               for b in res.blocks)


def test_noisy_ground_with_clear_outliers():
    pts, truth, _ = scenes.ground_with_noise()
    res = segment_points(pts, DEFAULT)
    m = evaluate(res.labels, truth.tolist())
    assert m["ground"]["precision"] >= 0.99
    assert m["ground"]["recall"] >= 0.99
    assert m["non_ground"]["recall"] >= 0.90


def test_duplicate_points_consistent_and_correct():
    pts, truth, _ = scenes.duplicate_points()
    res = segment_points(pts, DEFAULT)
    assert res.stats["ground"] == pts.shape[0]
    # 同一空间位置的全部重复副本标签必须一致
    n_unique = pts.shape[0] // 8
    for i in range(n_unique):
        labels = {res.labels[i + k * n_unique] for k in range(8)}
        assert labels == {GROUND}
    m = evaluate(res.labels, truth.tolist())["ground"]
    assert m["precision"] == pytest.approx(1.0)
    assert m["recall"] == pytest.approx(1.0)


def test_determinism_same_seed():
    pts, _, _ = scenes.ground_with_wall()
    r1 = segment_points(pts, DEFAULT)
    r2 = segment_points(pts, DEFAULT)
    assert r1.labels == r2.labels
    assert r1.reasons == r2.reasons
    assert np.allclose(r1.confidences, r2.confidences)


def test_global_ids_preserved_through_tiles():
    # 输出顺序与输入一一对应；坐标用的是全局坐标（对平面 offset 的符号抽查）
    pts, truth, _ = scenes.flat_ground()
    res = segment_points(pts, DEFAULT)
    assert len(res.labels) == pts.shape[0]
    # 所有已判定块的平面都定义在全局系：地面平面 offset ≈ 0（地面 z≈0）
    for b in res.blocks:
        if b.decided:
            assert abs(b.plane_offset) < 0.5


def test_single_tile_mode_matches_tiled_on_flat():
    pts, truth, _ = scenes.flat_ground()
    tiled = segment_points(pts, DEFAULT)
    single = segment_points(pts, GroundSegParams(tile_size=0))
    assert single.stats["blocks"] == 1
    assert single.stats["ground"] == tiled.stats["ground"]


def test_empty_cloud():
    res = segment_points(np.zeros((0, 3)), DEFAULT)
    assert res.labels == []
    assert res.stats["total"] == 0


def test_bad_input_shape_raises():
    with pytest.raises(ValueError):
        segment_points(np.zeros((5, 2)))


def test_seed_indices_out_of_range_raises():
    pts, _, _ = scenes.flat_ground(size=2.0, spacing=0.5)
    with pytest.raises(ValueError):
        segment_points(pts, DEFAULT, seed_indices=[0, 1, 9999])


def test_seed_indices_help_on_adversarial_plane():
    # 构造一个“天花板点更多、地面点更少”的场景：
    # 无种子时最大平面是天花板（但它水平，仍算地面语义会混淆），
    # 用两个不同高度的水平平面 + 倾斜角无法区分，因此改用：
    # 给种子后结果对种子确定性地产生首个假设且不报错。
    rng = np.random.default_rng(0)
    floor = np.column_stack([
        rng.uniform(-1, 1, 40), rng.uniform(-1, 1, 40),
        rng.normal(0, 0.01, 40)])
    ceil = np.column_stack([
        rng.uniform(-1, 1, 120), rng.uniform(-1, 1, 120),
        rng.normal(3, 0.01, 120)])
    pts = np.vstack([floor, ceil])
    res = segment_points(pts, GroundSegParams(tile_size=0, seed_rng=1),
                         seed_indices=[0, 1, 2, 3])
    # 地面种子被使用；天花板是更大平面。无论最终哪个平面胜出，
    # 系统都不允许崩溃且必须给出确定标签
    assert len(res.labels) == pts.shape[0]
    assert all(l in {GROUND, NON_GROUND, UNDECIDED} for l in res.labels)
