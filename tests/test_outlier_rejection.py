"""验收测试 4：离群剔除规则真实生效且逐轮可追溯。"""
from __future__ import annotations

import cv2
import numpy as np

from conftest import HEIGHT, WIDTH, post_calibration, render_set
from app import synth
from app.synth import encode_png


def _warp_image(img: np.ndarray, amp_px: float = 9.0) -> np.ndarray:
    """对图像施加局部正弦几何扭曲：棋盘整体仍可检测，但角点不服从全局针孔模型，
    从而产生真实的高重投影误差（而非单纯噪声，后者亚像素仍一致）。
    """
    h, w = img.shape[:2]
    xs, ys = np.meshgrid(np.arange(w), np.arange(h))
    map_x = (xs + amp_px * np.sin(ys / 90.0)).astype(np.float32)
    map_y = ys.astype(np.float32)
    return cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR,
                     borderValue=(200, 200, 200))


def test_warped_outlier_is_rejected_with_trace(client, camera_truth):
    K, dist = camera_truth
    poses = synth.diverse_poses(9, 6, 25.0, distance_mm=520.0, count=16, seed=9)

    files = render_set(K, dist, poses[:-1], noise=1.0)
    rvec, tvec = poses[-1]
    bad_img = synth.render_board(WIDTH, HEIGHT, 9, 6, 25.0, rvec, tvec, K, dist,
                                 ss=2, noise_sigma=2.0)
    bad_img = _warp_image(bad_img)
    # 前置条件：扭曲图必须仍能检测到角点（否则测的是角点失败而非离群剔除）
    found, _ = cv2.findChessboardCorners(
        cv2.cvtColor(bad_img, cv2.COLOR_BGR2GRAY), (9, 6))
    assert found
    files.append(("bad_warp.png", encode_png(bad_img)))

    resp = post_calibration(client, files)
    assert resp.status_code == 201, resp.text
    rec = resp.json()

    outlier_rows = [r for r in rec["rejected_images"] if r["stage"] == "outlier"]
    assert len(outlier_rows) >= 1
    row = outlier_rows[0]
    assert row["filename"] == "bad_warp.png"
    assert row["rms_px"] is not None and row["rms_px"] > 2.0
    assert "超过剔除阈值" in row["reason"]
    # 逐轮历史：记录了第几轮、阈值、中位数、剩余数量
    assert rec["rejection_history"]
    h0 = rec["rejection_history"][0]
    assert {"round", "removed_image_id", "rms_px", "median_rms_px",
            "threshold_px", "remaining"} <= set(h0)
    assert h0["rms_px"] > h0["threshold_px"]
    # 剔除后整体误差应保持在亚像素级
    assert rec["overall_rms_px"] < 1.0
    assert rec["accepted_count"] == 15
