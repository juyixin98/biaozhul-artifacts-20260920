"""SE2（平面刚体运动）基础运算。

位姿用 ``(x, y, theta)`` 表示，齐次变换为 ``T = [R(theta)  t; 0 1]``。
相对约束 ``z_ij`` 的含义：``T_j ≈ T_i * Z_ij``。
"""

from __future__ import annotations

import numpy as np

TWO_PI = 2.0 * np.pi

# 平面旋转的叉积矩阵：J @ v 等于把 v 逆时针旋转 90°
J_HAT = np.array([[0.0, -1.0], [1.0, 0.0]])


def wrap_angle(angle: float) -> float:
    """把任意角度归一化到 ``(-pi, pi]``。

    例如 +3.0π 与 -π 表示同一方向，残差必须先做这一步，
    否则跨 ±π 时会错误地产生接近 2π 的误差（即角度跨 pi 问题）。
    边界约定：``-π`` 归一化为 ``+π``（同一朝向）。
    """
    wrapped = (angle + np.pi) % TWO_PI - np.pi  # 结果在 [-pi, pi)
    if wrapped == -np.pi:
        return float(np.pi)
    return float(wrapped)


def rotation_matrix(theta: float) -> np.ndarray:
    """返回 2x2 旋转矩阵 ``R(theta)``。"""
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]], dtype=float)


def invert_pose(pose: np.ndarray) -> np.ndarray:
    """SE2 逆变换：``(R, t)^-1 = (R^T, -R^T t)``。"""
    x, y, theta = pose
    r_t = rotation_matrix(theta).T
    t_inv = -r_t @ np.array([x, y], dtype=float)
    return np.array([t_inv[0], t_inv[1], wrap_angle(-theta)])


def compose_pose(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """SE2 复合：``T_a * T_b``。"""
    ax, ay, ath = a
    bx, by, bth = b
    t = rotation_matrix(ath) @ np.array([bx, by], dtype=float) + np.array(
        [ax, ay], dtype=float
    )
    return np.array([t[0], t[1], wrap_angle(ath + bth)])


def pose_error(
    pose_i: np.ndarray, pose_j: np.ndarray, measurement: np.ndarray
) -> np.ndarray:
    """边残差 ``e = log(Z^-1 * T_i^-1 * T_j)``（表达在测量坐标系下）。

    返回 ``[e_x, e_y, e_theta]``，其中角度分量经 :func:`wrap_angle` 处理，
    保证跨 ±π 时取最短角差。
    """
    _, _, thi = pose_i
    _, _, thj = pose_j
    tz = measurement[:2]
    thz = measurement[2]

    r_i = rotation_matrix(thi)
    r_z = rotation_matrix(thz)
    delta_t = pose_j[:2] - pose_i[:2]

    # 估计相对位移 T_i^-1 T_j 的平移部分（在 i 坐标系下）
    p = r_i.T @ delta_t
    e_xy = r_z.T @ (p - tz)
    e_theta = wrap_angle(thj - thi - thz)
    return np.array([e_xy[0], e_xy[1], e_theta])


def edge_jacobians(
    pose_i: np.ndarray, pose_j: np.ndarray, measurement: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """残差对节点 i、j 的 3x3 解析雅可比 ``(A_i, B_j)``。

    推导见 README“数学说明”一节；单元测试中用有限差分数值校验。
    """
    thi = pose_i[2]
    r_i = rotation_matrix(thi)
    r_z = rotation_matrix(measurement[2])
    delta_t = pose_j[:2] - pose_i[:2]
    p = r_i.T @ delta_t

    m_block = r_z.T @ r_i.T  # 2x2
    a_i = np.zeros((3, 3))
    b_j = np.zeros((3, 3))

    a_i[:2, :2] = -m_block
    a_i[:2, 2] = r_z.T @ (-J_HAT @ p)
    a_i[2, 2] = -1.0

    b_j[:2, :2] = m_block
    b_j[2, 2] = 1.0
    return a_i, b_j
