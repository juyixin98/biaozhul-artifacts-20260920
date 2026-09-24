"""针孔投影模型及解析雅可比。

投影：u = fx * x/z + cx, v = fy * y/z + cy，其中 (x, y, z) 为相机坐标系下的点。

相机增量采用局部参数化（左扰动）：
    R <- Exp(dr) @ R,  t <- t + dt
因此 p_c' ≈ p_c - [p_c - t]_x @ dr + dt，
    d(p_c)/d(dr) = -[p_c - t]_x,  d(p_c)/d(dt) = I。
"""

from __future__ import annotations

import numpy as np

from .lie import skew
from .problem import CameraPose, Intrinsics


def project(intr: Intrinsics, pose: CameraPose, point: np.ndarray):
    """返回 (像素坐标 (2,), 相机系坐标 (3,))。"""
    pc = pose.transform(point)
    x, y, z = pc
    u = intr.fx * x / z + intr.cx
    v = intr.fy * y / z + intr.cy
    return np.array([u, v]), pc


def projection_jacobians(intr: Intrinsics, pose: CameraPose, point: np.ndarray):
    """返回 (J_cam (2,6), J_point (2,3))。

    J_cam 的列顺序为 (dr, dt)，即先旋转扰动后平移扰动。
    """
    pc = pose.transform(point)
    x, y, z = pc
    J_proj = np.array(
        [
            [intr.fx / z, 0.0, -intr.fx * x / (z * z)],
            [0.0, intr.fy / z, -intr.fy * y / (z * z)],
        ]
    )
    J_cam = np.hstack([J_proj @ (-skew(pc - pose.t)), J_proj])
    J_point = J_proj @ pose.R
    return J_cam, J_point
