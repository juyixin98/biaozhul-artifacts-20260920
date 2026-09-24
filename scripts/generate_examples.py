#!/usr/bin/env python3
"""生成 examples/ 下的示例棋盘格图片（合成投影，真实几何）。

用法：python scripts/generate_examples.py
产物：
  examples/good/1280x960/  view_00.png ... view_17.png  （姿态分散的合格样本）
  examples/duplicates/                  6 张近似重复视角
  examples/bad/                         损坏文件、空白图、无棋盘图
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.synth import (  # noqa: E402
    diverse_poses,
    encode_png,
    make_camera_matrix,
    nearly_identical_poses,
    render_board,
)

PATTERN_COLS, PATTERN_ROWS, SQUARE_MM = 9, 6, 25.0
WIDTH, HEIGHT = 1280, 960


def write_jpg(path, img, quality: int = 92) -> None:
    # JPEG q92：体积约为同图 PNG 的 1/7，且角点定位/标定质量不变（已验证）
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, quality])


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "examples"
    good_dir = root / "good" / "1280x960"
    dup_dir = root / "duplicates" / "1280x960"
    bad_dir = root / "bad"
    for d in (good_dir, dup_dir, bad_dir):
        d.mkdir(parents=True, exist_ok=True)
    # 重新生成时清空旧文件，避免 PNG/JPG 混杂
    for d in (good_dir, dup_dir):
        for old in d.glob("*"):
            old.unlink()

    # 合成相机：55° 视场 + 轻度桶形畸变（渲染端真值，标定端应能恢复）
    K = make_camera_matrix(WIDTH, HEIGHT, fov_deg=55.0)
    dist = np.array([[-0.12], [0.03], [0.0005], [-0.0005], [-0.005]], np.float64)

    poses = diverse_poses(PATTERN_COLS, PATTERN_ROWS, SQUARE_MM,
                          distance_mm=520.0, count=18)
    for i, (rvec, tvec) in enumerate(poses):
        img = render_board(WIDTH, HEIGHT, PATTERN_COLS, PATTERN_ROWS, SQUARE_MM,
                           rvec, tvec, K, dist, ss=2, noise_sigma=1.2)
        write_jpg(good_dir / f"view_{i:02d}.jpg", img)
    print(f"wrote {len(poses)} good views -> {good_dir}")

    for i, (rvec, tvec) in enumerate(nearly_identical_poses(distance_mm=520.0, count=6)):
        img = render_board(WIDTH, HEIGHT, PATTERN_COLS, PATTERN_ROWS, SQUARE_MM,
                           rvec, tvec, K, dist, ss=2, noise_sigma=1.0)
        write_jpg(dup_dir / f"dup_{i:02d}.jpg", img)
    print(f"wrote 6 near-duplicate views -> {dup_dir}")

    # 异常图：强模糊弱纹理（无棋盘）、平滑渐变（无角点）、截断文件、非图像字节
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 256, (HEIGHT // 8, WIDTH // 8, 3), dtype=np.uint8)
    noise = cv2.resize(noise, (WIDTH, HEIGHT), interpolation=cv2.INTER_LINEAR)
    noise = cv2.GaussianBlur(noise, (101, 101), 40.0)
    cv2.imwrite(str(bad_dir / "noise_no_board.png"), noise)
    gradient = np.repeat(np.tile(np.linspace(0, 255, WIDTH, dtype=np.uint8), (HEIGHT, 1))[:, :, None], 3, axis=2)
    cv2.imwrite(str(bad_dir / "gradient_no_board.png"), gradient)
    (bad_dir / "truncated.png").write_bytes(encode_png(gradient)[:120])  # 截断的 PNG
    (bad_dir / "not_an_image.jpg").write_bytes(b"this is definitely not image data")
    print(f"wrote 4 corrupt/boardless images -> {bad_dir}")

    # 元信息（声明棋盘与渲染真值，供阅读 README 时对照）
    (root / "README_META.txt").write_text(
        "synthetic checkerboard set\n"
        f"resolution: {WIDTH}x{HEIGHT}\n"
        f"board: {PATTERN_COLS}x{PATTERN_ROWS} inner corners, square={SQUARE_MM} mm\n"
        "render intrinsics (truth, NOT used by the calibrator):\n"
        f"fx=fy={K[0,0]:.4f} cx={K[0,2]:.2f} cy={K[1,2]:.2f}\n"
        "dist(k1,k2,p1,p2,k3)=(-0.12, 0.03, 0.0005, -0.0005, -0.005)\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
