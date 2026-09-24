"""验收测试 1：真实标定质量。

用合成投影夹具（内参真值对断言可见、对标定器不可见）验证：
* 内参 fx/fy/cx/cy 真实恢复且精度达标，不是固定矩阵；
* 逐图重投影误差全部报告且合理；
* 每张图位姿、输入 SHA-256 等溯源信息齐全；
* HMAC 签名可验证、篡改可检出（真实密码学）。
"""
from __future__ import annotations

import numpy as np

from conftest import HEIGHT, PATTERN, SQUARE_MM, WIDTH, post_calibration


def test_calibration_recovers_true_intrinsics(client, good_set):
    files, K_truth = good_set
    resp = post_calibration(client, files)
    assert resp.status_code == 201, resp.text
    rec = resp.json()

    assert rec["accepted_count"] == 18
    assert rec["rejected_count"] == 0
    intr = rec["intrinsics"]
    # 焦距真值 ~1229.4，恢复误差应远小于 1%（0.2px 渲染噪声下实测 <0.3%）
    assert abs(intr["fx"] - float(K_truth[0, 0])) / K_truth[0, 0] < 0.01
    assert abs(intr["fy"] - float(K_truth[1, 1])) / K_truth[1, 1] < 0.01
    # 主点应落在画面中心附近 1% 内
    assert abs(intr["cx"] - WIDTH / 2) < WIDTH * 0.01
    assert abs(intr["cy"] - HEIGHT / 2) < HEIGHT * 0.01
    # 分辨率与相机绑定
    assert rec["resolution"] == [WIDTH, HEIGHT]
    assert rec["board"] == {
        "pattern_cols": PATTERN[0], "pattern_rows": PATTERN[1],
        "square_size_mm": SQUARE_MM, "inner_corner_count": PATTERN[0] * PATTERN[1],
    }


def test_not_a_fixed_matrix(client, good_set):
    """两组不同 FOV 的数据必须得到不同内参——证明结果来自计算而非预置常量。"""
    files, _ = good_set
    rec1 = post_calibration(client, files, camera_id="camX").json()

    # 用更窄 FOV（更大焦距）重新渲染一整套
    from app import synth
    K2 = synth.make_camera_matrix(WIDTH, HEIGHT, fov_deg=40.0)
    dist2 = np.zeros((5, 1), np.float64)
    poses = synth.diverse_poses(PATTERN[0], PATTERN[1], SQUARE_MM,
                                distance_mm=520.0, count=18, seed=11)
    from conftest import render_set
    files2 = render_set(K2, dist2, poses, noise=1.0)
    rec2 = post_calibration(client, files2, camera_id="camY").json()

    f1, f2 = rec1["intrinsics"]["fx"], rec2["intrinsics"]["fx"]
    assert abs(f2 - f1) / f1 > 0.15  # FOV 55°->40°，焦距应变大约 45%
    assert abs(f2 - float(K2[0, 0])) / K2[0, 0] < 0.01


def test_per_image_reprojection_errors_reported(client, good_set):
    files, _ = good_set
    rec = post_calibration(client, files).json()
    per = rec["accepted_images"]
    assert len(per) == 18
    rms_values = [a["rms_px"] for a in per]
    # 每张图都有正的、有限的逐图误差，且互不相同（真实计算特征）
    assert all(r > 0 and np.isfinite(r) for r in rms_values)
    assert len(set(round(r, 6) for r in rms_values)) > 10
    # 整体 RMS 与逐图 RMS 一致量级（合成噪声约 0.2px）
    assert 0.02 < rec["overall_rms_px"] < 1.0
    for a in per:
        assert set(a["pose"]) == {"rvec", "tvec_mm"}
        assert len(a["pose"]["rvec"]) == 3 and len(a["pose"]["tvec_mm"]) == 3


def test_provenance_and_thresholds_present(client, good_set):
    files, _ = good_set
    rec = post_calibration(client, files).json()
    prov = rec["provenance"]
    assert prov["engine"] == "cv2.calibrateCamera"
    assert prov["opencv_version"]
    hashes = [f["sha256"] for f in prov["input_files"]]
    assert len(hashes) == 18
    assert all(len(h) == 64 and h != "0" * 64 for h in hashes)
    assert len(set(hashes)) == 18
    th = rec["thresholds"]
    assert {"min_views", "duplicate", "coverage", "outlier"} <= set(th)


def test_signature_is_real_hmac_and_tamper_detected(client, good_set):
    files, _ = good_set
    rec = post_calibration(client, files).json()
    vid = rec["version_id"]

    # 正常签名验证通过
    ok = client.post(f"/api/v1/versions/{vid}/verify").json()
    assert ok["valid"] is True
    assert len(ok["payload_sha256"]) == 64

    # 直接篡改落盘 JSON 中的焦距 -> 复算签名必须失败
    from app.config import settings
    import json
    from pathlib import Path
    record_path = Path(settings.storage_dir) / "versions" / f"{vid}.json"
    tampered = json.loads(record_path.read_text())
    tampered["intrinsics"]["fx"] = 9999.0
    record_path.write_text(json.dumps(tampered))

    bad = client.post(f"/api/v1/versions/{vid}/verify").json()
    assert bad["valid"] is False
    assert "篡改" in bad["reason"]
