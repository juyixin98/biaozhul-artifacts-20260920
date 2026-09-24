"""合成场景生成：已知真值的相机轨迹 + 三维点云 + 加噪投影观测。

用于验收：真值完全已知，可对比优化结果与真值。
"""

from __future__ import annotations

import numpy as np

from .lie import exp_so3
from .problem import BAProblem, CameraPose, Intrinsics, Observation
from .projection import project


def simulate_scene(
    n_cameras: int = 5,
    n_points: int = 40,
    noise_px: float = 1.0,
    seed: int = 0,
    image_size: tuple[int, int] = (640, 480),
    perturb_rot: float = 0.02,
    perturb_trans: float = 0.1,
    perturb_point: float = 0.3,
    n_behind_camera: int = 0,
    n_under_observed: int = 0,
) -> dict:
    """生成合成束调整问题。

    返回字典：
      intrinsics   : Intrinsics（固定内参）
      gt_cameras   : 真值位姿列表
      gt_points    : (N, 3) 真值点
      init_cameras : 加扰动的初始位姿（相机 0 不扰动，作为锚）
      init_points  : 加扰动的初始点
      observations : 加噪观测列表
    """
    rng = np.random.default_rng(seed)
    w, h = image_size
    intr = Intrinsics(fx=500.0, fy=500.0, cx=w / 2.0, cy=h / 2.0)

    # 真值相机：沿 x 轴平移并带小旋转，朝向 +z 方向的点云
    gt_cameras: list[CameraPose] = []
    for i in range(n_cameras):
        yaw = 0.05 * np.sin(i)
        pitch = 0.02 * np.cos(i * 1.3)
        R = exp_so3(np.array([pitch, yaw, 0.0]))
        t = np.array([-0.5 * i, 0.05 * np.sin(2 * i), 0.0])
        gt_cameras.append(CameraPose(R=R, t=t))

    # 真值点：相机前方的随机点云
    gt_points = np.column_stack(
        [
            rng.uniform(-2.0, 2.0, n_points),
            rng.uniform(-1.5, 1.5, n_points),
            rng.uniform(3.0, 8.0, n_points),
        ]
    )

    # 负深度点：位于相机 0 后方（z < 0），其"观测"为代数投影（物理上不可见）
    behind_points = np.empty((0, 3))
    if n_behind_camera > 0:
        behind_points = np.column_stack(
            [
                rng.uniform(-2.0, 2.0, n_behind_camera),
                rng.uniform(-1.5, 1.5, n_behind_camera),
                rng.uniform(-5.0, -2.0, n_behind_camera),
            ]
        )
        gt_points = np.vstack([gt_points, behind_points])

    # 观测不足点：只被一台相机看到
    under_points = np.empty((0, 3))
    if n_under_observed > 0:
        under_points = np.column_stack(
            [
                rng.uniform(-2.0, 2.0, n_under_observed),
                rng.uniform(-1.5, 1.5, n_under_observed),
                rng.uniform(3.0, 8.0, n_under_observed),
            ]
        )
        gt_points = np.vstack([gt_points, under_points])

    # 生成观测（加高斯噪声，仅保留在图像内且深度为正的投影）
    observations: list[Observation] = []
    n_main = n_points
    for ci, cam in enumerate(gt_cameras):
        for pj in range(n_main):
            pc = cam.transform(gt_points[pj])
            if pc[2] < 0.5:
                continue
            uv, _ = project(intr, cam, gt_points[pj])
            if not (0 <= uv[0] < w and 0 <= uv[1] < h):
                continue
            uv_noisy = uv + rng.normal(0.0, noise_px, 2)
            observations.append(Observation(ci, pj, uv_noisy))

    # 负深度点的伪观测（代数投影，用于验证求解器的剔除逻辑）
    for pj in range(n_main, n_main + n_behind_camera):
        cam = gt_cameras[0]
        uv, _ = project(intr, cam, gt_points[pj])
        observations.append(Observation(0, pj, uv + rng.normal(0.0, noise_px, 2)))

    # 观测不足点：仅相机 0 观测
    for pj in range(n_main + n_behind_camera, len(gt_points)):
        cam = gt_cameras[0]
        uv, _ = project(intr, cam, gt_points[pj])
        observations.append(Observation(0, pj, uv + rng.normal(0.0, noise_px, 2)))

    # 初始估计：真值加扰动（相机 0 固定为真值，作为锚）
    init_cameras = []
    for i, cam in enumerate(gt_cameras):
        if i == 0:
            init_cameras.append(CameraPose(cam.R.copy(), cam.t.copy()))
        else:
            dR = exp_so3(rng.normal(0.0, perturb_rot, 3))
            dt = rng.normal(0.0, perturb_trans, 3)
            init_cameras.append(CameraPose(dR @ cam.R, cam.t + dt))
    init_points = gt_points + rng.normal(0.0, perturb_point, gt_points.shape)

    return {
        "intrinsics": intr,
        "gt_cameras": gt_cameras,
        "gt_points": gt_points,
        "init_cameras": init_cameras,
        "init_points": init_points,
        "observations": observations,
        "image_size": image_size,
    }


def scene_to_problem(scene: dict, use_ground_truth: bool = False) -> BAProblem:
    """把 simulate_scene 的输出组装成 BAProblem（默认用扰动初值）。"""
    cams = scene["gt_cameras"] if use_ground_truth else scene["init_cameras"]
    pts = scene["gt_points"] if use_ground_truth else scene["init_points"]
    return BAProblem(
        intrinsics=scene["intrinsics"],
        cameras=[CameraPose(c.R.copy(), c.t.copy()) for c in cams],
        points=np.array(pts, dtype=float).copy(),
        observations=list(scene["observations"]),
    )
