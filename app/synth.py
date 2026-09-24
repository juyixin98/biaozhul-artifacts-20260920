"""合成棋盘格投影夹具：用真实针孔相机模型 + OpenCV 透视投影渲染图像。

用途：
1. tests/ 验收测试的确定性数据源（内参已知，可反查标定是否真实计算）；
2. 生成 examples/ 示例输入。

渲染流程：在世界平面 z=0 上铺棋盘多边形 -> 按 rvec/tvec + K + 畸变投影到像素
-> 在 2 倍超采样画布上填充 -> 高斯模糊后降采样（抗锯齿，角点亚像素可精化）。
"""
from __future__ import annotations

import math

import cv2
import numpy as np


def make_camera_matrix(width: int, height: int, fov_deg: float = 55.0) -> np.ndarray:
    fx = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    return np.array(
        [[fx, 0.0, width / 2.0],
         [0.0, fx, height / 2.0],
         [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def pose_from_euler_xyz(
    roll_deg: float, pitch_deg: float, yaw_deg: float,
    distance_mm: float, target_xy_mm: tuple[float, float] = (0.0, 0.0),
) -> tuple[np.ndarray, np.ndarray]:
    """由欧拉角（度）与相机距离构造 rvec/tvec。

    相机在光轴方向距棋盘 distance_mm，旋转后 t 取反变换；target_xy 为光轴
    瞄准点（相对棋盘中心，单位 mm）。
    """
    roll = math.radians(roll_deg)
    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)

    Rx = np.array([[1, 0, 0], [0, math.cos(roll), -math.sin(roll)],
                   [0, math.sin(roll), math.cos(roll)]])
    Ry = np.array([[math.cos(pitch), 0, math.sin(pitch)], [0, 1, 0],
                   [-math.sin(pitch), 0, math.cos(pitch)]])
    Rz = np.array([[math.cos(yaw), -math.sin(yaw), 0],
                   [math.sin(yaw), math.cos(yaw), 0], [0, 0, 1]])
    R = Rx @ Ry @ Rz
    # 相机中心：在棋盘前方 z 轴，先偏移使瞄准点为 target_xy
    cam_center = R.T @ np.array([0.0, 0.0, distance_mm]) + np.array(
        [target_xy_mm[0], target_xy_mm[1], 0.0]
    )
    t = -R @ cam_center
    rvec, _ = cv2.Rodrigues(R)
    return rvec.astype(np.float64), t.reshape(3, 1).astype(np.float64)


def render_board(
    width: int,
    height: int,
    pattern_cols: int,
    pattern_rows: int,
    square_size_mm: float,
    rvec: np.ndarray,
    tvec: np.ndarray,
    camera_matrix: np.ndarray,
    dist_coeffs: np.ndarray | None = None,
    ss: int = 2,
    noise_sigma: float = 0.0,
) -> np.ndarray:
    """渲染单张棋盘图。pattern_cols/rows 为内角点数，因此格子数为 +1。"""
    if dist_coeffs is None:
        dist_coeffs = np.zeros((5, 1), np.float64)

    W, H = width * ss, height * ss
    canvas = np.full((H, W, 3), 200, np.uint8)
    k_ss = camera_matrix.copy() * ss
    k_ss[2, 2] = 1.0

    n_cols_cells = pattern_cols + 1
    n_rows_cells = pattern_rows + 1
    board_w = n_cols_cells * square_size_mm
    board_h = n_rows_cells * square_size_mm
    origin_x, origin_y = -board_w / 2.0, -board_h / 2.0

    for r in range(n_rows_cells):
        for c in range(n_cols_cells):
            x0 = origin_x + c * square_size_mm
            y0 = origin_y + r * square_size_mm
            corners3 = np.array(
                [[x0, y0, 0],
                 [x0 + square_size_mm, y0, 0],
                 [x0 + square_size_mm, y0 + square_size_mm, 0],
                 [x0, y0 + square_size_mm, 0]],
                dtype=np.float64,
            ).reshape(-1, 1, 3)
            projected, _ = cv2.projectPoints(corners3, rvec, tvec, k_ss, dist_coeffs)
            poly = projected.reshape(-1, 2)
            if np.any(~np.isfinite(poly)):
                continue
            shade = 30 if (r + c) % 2 == 0 else 225
            cv2.fillConvexPoly(canvas, np.int32(poly), (shade, shade, shade),
                               lineType=cv2.LINE_AA)

    # 轻微模糊 + 降采样，产生自然抗锯齿边缘
    canvas = cv2.GaussianBlur(canvas, (3, 3), 0.6 * ss)
    img = cv2.resize(canvas, (width, height), interpolation=cv2.INTER_AREA)
    if noise_sigma > 0:
        noise = np.random.normal(0, noise_sigma, img.shape)
        img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return img


# --------------------------------------------------------------------------- #
# 验收用姿态集合
# --------------------------------------------------------------------------- #
def diverse_poses(
    pattern_cols: int, pattern_rows: int, square_size_mm: float,
    distance_mm: float = 520.0, count: int = 18, seed: int = 7,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """生成方向、俯仰、横滚和瞄准位置都分散的 count 个位姿（确定性）。

    棋盘较近、倾角较大，使畸变在径向上充分可观测（标定文献推荐姿态）。
    """
    rng = np.random.default_rng(seed)
    board_w = (pattern_cols + 1) * square_size_mm
    board_h = (pattern_rows + 1) * square_size_mm
    poses: list[tuple[np.ndarray, np.ndarray]] = []
    yaws = np.linspace(-36, 36, 6)
    pitches = np.linspace(-26, 26, 5)
    grid = [(float(y), float(p)) for p in pitches for y in yaws]
    rng.shuffle(grid)
    for i, (yaw, pitch) in enumerate(grid[:count]):
        roll = float(rng.uniform(-12, 12))
        dist = distance_mm * float(rng.uniform(0.85, 1.15))
        tx = float(rng.uniform(-0.12, 0.12)) * board_w
        ty = float(rng.uniform(-0.12, 0.12)) * board_h
        poses.append(
            pose_from_euler_xyz(roll, pitch, yaw, dist, (tx, ty))
        )
    return poses


def nearly_identical_poses(
    base_yaw: float = 5.0, base_pitch: float = -3.0,
    distance_mm: float = 900.0, count: int = 6,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """姿态差异极小的视图集合，用于触发重复视角/覆盖退化规则。"""
    return [
        pose_from_euler_xyz(0.1 * i, base_pitch + 0.1 * i, base_yaw + 0.1 * i,
                            distance_mm + 2.0 * i)
        for i in range(count)
    ]


def degenerate_poses(
    count: int = 6, distance_mm: float = 520.0, seed: int = 3,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """视角彼此【不重复】（距离/瞄准位置差异大）但方向高度集中的姿态。

    用于专门触发姿态覆盖退化：最大视角夹角 <12°，却不会被重复视角聚类提前剔除。
    """
    rng = np.random.default_rng(seed)
    poses = []
    for i in range(count):
        yaw = float(np.linspace(-3.5, 3.5, count)[i])
        pitch = float(np.linspace(-2.0, 2.0, count)[(i + 2) % count])
        roll = float(rng.uniform(-2, 2))
        dist = distance_mm * (0.62 + 0.62 * (i / max(count - 1, 1)))  # 0.62x..1.24x
        # 瞄准点大幅变化 -> 投影中心差异大，保证不被判为重复视角
        tx = (0.22 if i % 2 == 0 else -0.22) * 9 * 25.0
        ty = (0.18 if i % 3 == 0 else -0.18) * 6 * 25.0
        poses.append(pose_from_euler_xyz(roll, pitch, yaw, dist, (tx, ty)))
    return poses


def encode_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("PNG 编码失败")
    return buf.tobytes()
