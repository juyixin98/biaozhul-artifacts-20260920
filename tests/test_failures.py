"""验收测试 2：失败路径必须返回明确原因与证据，绝不静默出结果。

覆盖：异常图（解码/无棋盘）、重复视角、姿态覆盖退化、错误棋盘尺寸、
样本不足、同批分辨率混合。
"""
from __future__ import annotations

from conftest import post_calibration, render_set
from app import synth


def test_corrupt_and_boardless_images_rejected_but_calibration_succeeds(client, good_set, bad_images):
    """异常图被逐张标记原因，合格图仍能完成标定（样本选择可追溯）。"""
    files, _ = good_set
    resp = post_calibration(client, files + bad_images)
    assert resp.status_code == 201, resp.text
    rec = resp.json()
    assert rec["accepted_count"] == 18
    assert rec["rejected_count"] == 4
    stages = sorted({r["stage"] for r in rec["rejected_images"]})
    assert set(stages) <= {"decode", "corner"}
    assert any(r["stage"] == "decode" for r in rec["rejected_images"])
    for r in rec["rejected_images"]:
        assert r["reason"]
        assert r["image_id"] and r["filename"]


def test_all_corrupt_images_returns_unreadable(client):
    """所有文件都无法解码时，返回 ALL_IMAGES_UNREADABLE 并逐张说明。"""
    unreadable = [
        ("garbage1.jpg", b"this is definitely not image data"),
        ("garbage2.png", b"still not an image, just text bytes"),
        ("garbage3.png", bytes(range(32))),
        ("garbage4.png", b"\x89PNG\r\n\x1a\n-total-garbage-trailer-0123456789"),
    ]
    resp = post_calibration(client, unreadable)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "ALL_IMAGES_UNREADABLE"
    assert len(err["rejected_images"]) == 4
    assert all(r["stage"] == "decode" for r in err["rejected_images"])


def test_wrong_board_dimensions_detected_with_diagnostic(client, good_set):
    """声明 7x6（真实 9x6，列数少 2）必须失败，并给出邻近可检测尺寸的诊断线索。"""
    files, _ = good_set
    resp = post_calibration(client, files[:6], board=(7, 6))
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "INSUFFICIENT_SAMPLES"
    # 角点失败项中应包含诊断扫描结果（至少对部分图片）
    corner_failures = [r for r in err["rejected_images"] if r["stage"] == "corner"]
    assert corner_failures
    diagnosed = [r for r in corner_failures if r["detail"].get("nearby_boards_detected")]
    assert diagnosed, "应至少有一张失败图附带邻近棋盘尺寸诊断"
    dims = {(d["pattern_cols"], d["pattern_rows"])
            for r in diagnosed for d in r["detail"]["nearby_boards_detected"]}
    assert (9, 6) in dims


def test_transposed_board_is_geometrically_congruent(client, good_set):
    """行列填反（6x9）：6x9 与 9x6 棋盘旋转全等，检测/标定可正常完成，不产生 500。

    这里断言的是服务对该输入行为确定、结果可用（RMS 正常）；真正的“错误棋盘”
    用非全等的 (7,6) 用例覆盖。
    """
    files, _ = good_set
    resp = post_calibration(client, files, board=(6, 9), min_views=5)
    assert resp.status_code in (201, 422)
    if resp.status_code == 201:
        rec = resp.json()
        assert rec["accepted_count"] >= 5
        assert rec["overall_rms_px"] < 1.0


def test_duplicate_views_are_clustered(client, camera_truth):
    """6 张几乎相同的视角 + 少量正常视角：重复项必须被识别并给出代表样本。"""
    K, dist = camera_truth
    dup_poses = synth.nearly_identical_poses(distance_mm=520.0, count=6)
    good_poses = synth.diverse_poses(9, 6, 25.0, distance_mm=520.0, count=12, seed=5)
    files = render_set(K, dist, dup_poses + good_poses, noise=1.0)
    resp = post_calibration(client, files, min_views=14)
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "INSUFFICIENT_DISTINCT_VIEWS"
    dup_rejected = [r for r in err["rejected_images"] if r["stage"] == "duplicate"]
    # 6 张近似视角应聚成 1 个簇：保留 1 张、剔除约 5 张（可能吸收附近姿态）
    assert len(dup_rejected) >= 4
    for r in dup_rejected:
        assert r["detail"]["representative_image_id"]
        assert "视角重复" in r["reason"]


def test_degenerate_poses_rejected(client, camera_truth):
    """视角不重复但姿态方向高度集中时，必须以姿态覆盖退化拒绝。"""
    K, dist = camera_truth
    poses = synth.degenerate_poses(count=6)
    files = render_set(K, dist, poses, noise=0.8)
    resp = post_calibration(client, files, min_views=5)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "POSE_DEGENERACY"
    cov = err["coverage"]
    assert cov["max_pairwise_rotation_deg"] < 12.0
    assert err["reason"]


def test_insufficient_sample_count(client, camera_truth):
    """低于硬性下限（3 张）在解码前即被拒绝。"""
    K, dist = camera_truth
    poses = synth.diverse_poses(9, 6, 25.0, distance_mm=520.0, count=4)
    files = render_set(K, dist, poses[:2])
    resp = post_calibration(client, files)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INSUFFICIENT_SAMPLES"


def test_mixed_resolution_rejected(client, camera_truth):
    """标定版本绑定单一分辨率：混入其他尺寸图片必须被逐张标记 resolution。"""
    K, dist = camera_truth
    poses = synth.diverse_poses(9, 6, 25.0, distance_mm=520.0, count=18)
    files = render_set(K, dist, poses, noise=1.0)
    # 追加两张不同分辨率的同棋盘图
    K2 = synth.make_camera_matrix(1024, 768, fov_deg=55.0)
    small = render_set(K2, dist, poses[:2], noise=1.0, width=1024, height=768)
    resp = post_calibration(client, files + small)
    assert resp.status_code == 201, resp.text
    rec = resp.json()
    res_rejected = [r for r in rec["rejected_images"] if r["stage"] == "resolution"]
    assert len(res_rejected) == 2
    assert rec["resolution"] == [1280, 960]


def test_invalid_board_params_rejected(client, good_set):
    files, _ = good_set
    resp = post_calibration(client, files, board=(2, 6))
    assert resp.status_code == 422  # FastAPI 表单校验或业务校验


def test_high_min_views_requires_more_distinct_views(client, camera_truth):
    K, dist = camera_truth
    poses = synth.diverse_poses(9, 6, 25.0, distance_mm=520.0, count=8)
    files = render_set(K, dist, poses, noise=1.0)
    resp = post_calibration(client, files, min_views=10)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INSUFFICIENT_DISTINCT_VIEWS"
