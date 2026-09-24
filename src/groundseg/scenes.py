"""人工构造的合成点云场景，全部带逐点真值。

每个场景返回 (points, truth, meta)：
  points (N,3) float64 全局坐标
  truth  (N,)   —— 1=地面点，0=非地面点
  meta   dict   —— 场景参数（倾斜角、噪声水平等）

所有随机都走显式种子，场景可复现。场景设计刻意覆盖需求点名的
斜坡、垂直墙、稀疏点、噪声、重复点五类困难情形。
"""

from __future__ import annotations

import numpy as np


def _grid(nx: int, ny: int, size: float, rng: np.random.Generator,
          jitter: float = 0.0) -> np.ndarray:
    """size x size 的规则 XY 网格。"""
    xs = np.linspace(-size / 2, size / 2, nx)
    ys = np.linspace(-size / 2, size / 2, ny)
    xx, yy = np.meshgrid(xs, ys)
    pts = np.column_stack([xx.ravel(), yy.ravel(), np.zeros(nx * ny)])
    if jitter:
        pts[:, 0] += rng.normal(0, jitter, pts.shape[0])
        pts[:, 1] += rng.normal(0, jitter, pts.shape[0])
    return pts


def flat_ground(size: float = 8.0, spacing: float = 0.25,
                noise_std: float = 0.01, seed: int = 101):
    """水平地面 + 轻微测量噪声，全部为地面真值。"""
    rng = np.random.default_rng(seed)
    n = int(size / spacing)
    pts = _grid(n, n, size, rng, jitter=spacing * 0.02)
    pts[:, 2] = rng.normal(0, noise_std, pts.shape[0])
    truth = np.ones(pts.shape[0], dtype=np.int64)
    return pts, truth, {"scene": "flat_ground", "noise_std": noise_std}


def sloped_ground(angle_deg: float = 10.0, axis: str = "x",
                  size: float = 8.0, spacing: float = 0.25,
                  noise_std: float = 0.01, clutter: int = 200,
                  seed: int = 102):
    """单一倾角的平整斜坡（绕 y 或 x 轴倾斜）+ 可选空中杂点。

    angle_deg=10 用于“可接受斜坡应被识别”；
    angle_deg=35 配合 max_tilt=20 用于“陡坡必须拒判为 undecidable”。
    """
    rng = np.random.default_rng(seed)
    n = int(size / spacing)
    pts = _grid(n, n, size, rng, jitter=spacing * 0.02)
    theta = np.radians(angle_deg)
    if axis == "x":  # 沿 X 抬升
        pts[:, 2] = pts[:, 0] * np.tan(theta)
    else:
        pts[:, 2] = pts[:, 1] * np.tan(theta)
    pts[:, 2] += rng.normal(0, noise_std, pts.shape[0])
    truth = np.ones(pts.shape[0], dtype=np.int64)

    if clutter:
        # 在斜坡上方撒一批非地面杂点（高度明显偏离斜面）
        cx = rng.uniform(-size / 3, size / 3, clutter)
        cy = rng.uniform(-size / 3, size / 3, clutter)
        base_z = (cx * np.tan(theta)) if axis == "x" else (cy * np.tan(theta))
        cz = base_z + rng.uniform(0.8, 2.5, clutter)
        cl = np.column_stack([cx, cy, cz])
        pts = np.vstack([pts, cl])
        truth = np.concatenate([truth, np.zeros(clutter, dtype=np.int64)])
    return pts, truth, {"scene": "sloped_ground", "angle_deg": angle_deg,
                        "clutter": clutter}


def ground_with_wall(size: float = 8.0, spacing: float = 0.25,
                     wall_height: float = 2.5, wall_thickness: float = 0.1,
                     wall_rows: int = 26, noise_std: float = 0.01,
                     seed: int = 103):
    """水平地面 + 一道竖直薄墙（墙沿 X 方向居中横在 y=0）。

    墙点与地面点数量相当，但墙在 XY 上只占窄条，切块后墙会单独落进
    以墙为核心的块里——该块拟合出的竖直平面必须因倾角门槛被拒判，
    而不是“最大平面就是地面”。
    """
    rng = np.random.default_rng(seed)
    n = int(size / spacing)
    gnd = _grid(n, n, size, rng, jitter=spacing * 0.02)
    gnd[:, 2] = rng.normal(0, noise_std, gnd.shape[0])
    truth_g = np.ones(gnd.shape[0], dtype=np.int64)

    wx = np.linspace(-size / 2.4, size / 2.4, wall_rows)
    wz = np.linspace(0.30, wall_height, wall_rows)
    wxx, wzz = np.meshgrid(wx, wz)
    wall = np.column_stack([
        wxx.ravel(),
        rng.normal(0, wall_thickness, wxx.size),
        wzz.ravel() + rng.normal(0, noise_std, wxx.size),
    ])
    truth_w = np.zeros(wall.shape[0], dtype=np.int64)
    pts = np.vstack([gnd, wall])
    truth = np.concatenate([truth_g, truth_w])
    return pts, truth, {"scene": "ground_with_wall", "wall_height": wall_height}


def sparse_ground(n: int = 9, size: float = 8.0, seed: int = 104):
    """极稀疏地面点（默认 9 个），低于 min_unique_points 门槛，
    应返回 undecidable 而不是乱拟合。"""
    rng = np.random.default_rng(seed)
    pts = np.column_stack([
        rng.uniform(-size / 2, size / 2, n),
        rng.uniform(-size / 2, size / 2, n),
        rng.normal(0, 0.02, n),
    ])
    return pts, np.ones(n, dtype=np.int64), {"scene": "sparse_ground", "n": n}


def diffuse_noise(n: int = 400, size: float = 8.0, height: float = 3.0,
                  seed: int = 105):
    """空间中均匀散布的纯噪声点，无任何平面结构。

    RANSAC 任何平面的内点比例都应低于 min_inlier_ratio，
    结果必须是 undecidable，绝不能把随机最大平面当地面。"""
    rng = np.random.default_rng(seed)
    pts = np.column_stack([
        rng.uniform(-size / 2, size / 2, n),
        rng.uniform(-size / 2, size / 2, n),
        rng.uniform(0, height, n),
    ])
    return pts, np.ones(n, dtype=np.int64), {"scene": "diffuse_noise", "n": n}
    # 真值全部为地面点（模拟“希望找地面但场景里根本没有地面结构”）：
    # 一个合格的系统应当拒判，而不是硬找一个平面。


def ground_with_noise(size: float = 8.0, spacing: float = 0.25,
                      noise_std: float = 0.05, outliers: int = 150,
                      seed: int = 106):
    """水平地面叠加大测量噪声 + 一定比例的离群点。"""
    rng = np.random.default_rng(seed)
    n = int(size / spacing)
    gnd = _grid(n, n, size, rng, jitter=spacing * 0.02)
    gnd[:, 2] = rng.normal(0, noise_std, gnd.shape[0])
    truth_g = np.ones(gnd.shape[0], dtype=np.int64)

    ox = rng.uniform(-size / 2, size / 2, outliers)
    oy = rng.uniform(-size / 2, size / 2, outliers)
    # 离群点的 z 刻意避开地面距离门限附近（±0.3），保证真值无歧义：
    # 一半在地面上方明显处，一半在地面下方明显处。
    half = outliers // 2
    oz_up = rng.uniform(0.4, 3.0, half)
    oz_dn = rng.uniform(-3.0, -0.4, outliers - half)
    oz = np.concatenate([oz_up, oz_dn])
    rng.shuffle(oz)
    out = np.column_stack([ox, oy, oz])
    truth_o = np.zeros(outliers, dtype=np.int64)
    pts = np.vstack([gnd, out])
    return pts, np.concatenate([truth_g, truth_o]), {
        "scene": "ground_with_noise", "noise_std": noise_std, "outliers": outliers}


def duplicate_points(size: float = 8.0, spacing: float = 0.4,
                     dup_factor: int = 8, noise_std: float = 0.01,
                     seed: int = 107):
    """地面点每个位置重复 dup_factor 份（含微小抖动模拟重复扫描）。

    去重逻辑必须保证：重复点不会虚增内点计数 / 造成采样退化，
    且全部重复副本都得到一致的地面标签。"""
    rng = np.random.default_rng(seed)
    n = int(size / spacing)
    base = _grid(n, n, size, rng, jitter=0.0)
    base[:, 2] = rng.normal(0, noise_std, base.shape[0])
    copies = [base]
    for i in range(dup_factor - 1):
        c = base + rng.normal(0, 1e-5, base.shape)
        copies.append(c)
    pts = np.vstack(copies)
    truth = np.ones(pts.shape[0], dtype=np.int64)
    return pts, truth, {"scene": "duplicate_points", "dup_factor": dup_factor}


SCENES = {
    "flat_ground": flat_ground,
    "sloped_ground": sloped_ground,
    "steep_slope": lambda **kw: sloped_ground(angle_deg=35.0, seed=108, **kw),
    "ground_with_wall": ground_with_wall,
    "sparse_ground": sparse_ground,
    "diffuse_noise": diffuse_noise,
    "ground_with_noise": ground_with_noise,
    "duplicate_points": duplicate_points,
}
