"""边界情况：负深度点、观测不足、尺度歧义。"""

import numpy as np

from ba.problem import BAProblem, CameraPose, Intrinsics, Observation
from ba.simulate import scene_to_problem, simulate_scene
from ba.solver import BASolver, SolverOptions


# ---------------------------------------------------------------------- #
# 负深度点
# ---------------------------------------------------------------------- #
def test_negative_depth_observations_excluded():
    """相机后方的点（z<0）产生的观测必须被剔除并计入诊断。"""
    scene = simulate_scene(n_cameras=4, n_points=20, seed=2, n_behind_camera=3)
    problem = scene_to_problem(scene)
    res = BASolver(SolverOptions()).solve(problem)
    assert res.diagnostics["num_negative_depth_excluded"] == 3
    assert res.converged


def test_negative_depth_jacobian_guard():
    """直接构造负深度观测：求解器不应崩溃，且该观测不影响解。"""
    intr = Intrinsics(fx=500.0, fy=500.0, cx=320.0, cy=240.0)
    cams = [CameraPose(np.eye(3), np.zeros(3)), CameraPose(np.eye(3), np.array([-1.0, 0.0, 0.0]))]
    pts = np.array([[0.0, 0.0, 5.0], [0.0, 0.0, -3.0]])  # 第二个点在相机后方
    obs = [
        Observation(0, 0, np.array([320.0, 240.0])),
        Observation(1, 0, np.array([320.0, 240.0])),
        Observation(0, 1, np.array([100.0, 100.0])),  # 负深度观测
    ]
    problem = BAProblem(intr, cams, pts, obs)
    res = BASolver(SolverOptions()).solve(problem)
    assert res.diagnostics["num_negative_depth_excluded"] == 1


# ---------------------------------------------------------------------- #
# 观测不足
# ---------------------------------------------------------------------- #
def test_under_observed_points_flagged():
    """只被一台相机看到的点无法三角化：应被诊断标记，求解不崩溃。"""
    scene = simulate_scene(n_cameras=4, n_points=20, seed=3, n_under_observed=2)
    problem = scene_to_problem(scene)
    res = BASolver(SolverOptions()).solve(problem)
    flagged = res.diagnostics["under_observed_points"]
    assert len(flagged) == 2  # 两个观测不足点都被识别
    assert res.converged


def test_point_seen_once_does_not_break_lm():
    """单观测点的 3x3 块奇异，LM 阻尼应保证系统可解。"""
    intr = Intrinsics(fx=500.0, fy=500.0, cx=320.0, cy=240.0)
    cams = [CameraPose(np.eye(3), np.zeros(3)), CameraPose(np.eye(3), np.array([-1.0, 0.0, 0.0]))]
    pts = np.array([[0.0, 0.0, 5.0], [0.5, 0.0, 6.0]])
    obs = [
        Observation(0, 0, np.array([320.0, 240.0])),
        Observation(1, 0, np.array([320.0, 240.0])),
        Observation(0, 1, np.array([361.0, 240.0])),  # 点 1 仅一次观测
    ]
    problem = BAProblem(intr, cams, pts, obs)
    res = BASolver(SolverOptions(max_iterations=10)).solve(problem)
    assert 1 in res.diagnostics["under_observed_points"]
    assert np.isfinite(res.cost_history[-1])


# ---------------------------------------------------------------------- #
# 尺度歧义
# ---------------------------------------------------------------------- #
def _scaled_problem(problem: BAProblem, s: float) -> BAProblem:
    """把初值整体缩放 s 倍（点 + 相机平移），模拟不同初始尺度。"""
    out = problem.copy()
    out.points = out.points * s
    for c in out.cameras:
        c.t = c.t * s
    return out


def test_scale_ambiguity_without_anchor():
    """无尺度锚定时，单目 BA 的代价对全局尺度不变（规范自由度）。"""
    scene = simulate_scene(n_cameras=4, n_points=25, noise_px=0.5, seed=4)
    base = scene_to_problem(scene)
    opts = SolverOptions(scale_anchor=False, max_iterations=50)

    costs = []
    for s in (1.0, 2.0, 5.0):
        res = BASolver(opts).solve(_scaled_problem(base, s))
        costs.append(res.cost_history[-1])
    # 不同初始尺度收敛到（几乎）相同的代价 -> 尺度不可观测
    np.testing.assert_allclose(costs[1], costs[0], rtol=1e-6)
    np.testing.assert_allclose(costs[2], costs[0], rtol=1e-6)


def test_scale_anchor_recovers_scale():
    """启用尺度锚定后，||t_1|| 被钉在目标值，解的尺度与真值一致。"""
    scene = simulate_scene(n_cameras=5, n_points=30, noise_px=0.5, seed=5)
    problem = scene_to_problem(scene)
    gt_t1_norm = float(np.linalg.norm(scene["gt_cameras"][1].t))

    res = BASolver(SolverOptions(scale_anchor=True)).solve(problem)
    final = res.diagnostics["scale_anchor"]["final_t1_norm"]
    # 锚定目标 = 初始 ||t_1||（真值加扰动），强权重下终值应紧贴目标
    target = res.diagnostics["scale_anchor"]["target"]
    np.testing.assert_allclose(final, target, rtol=1e-6)
    # 初始扰动很小，因此恢复尺度应接近真值尺度
    np.testing.assert_allclose(final, gt_t1_norm, rtol=0.2)


def test_scale_anchor_forces_scaled_init_back():
    """即使初值整体放大 3 倍，锚定（目标=真值尺度）也能把尺度拉回。"""
    scene = simulate_scene(n_cameras=5, n_points=30, noise_px=0.5, seed=6)
    problem = _scaled_problem(scene_to_problem(scene), 3.0)
    gt_t1_norm = float(np.linalg.norm(scene["gt_cameras"][1].t))

    res = BASolver(SolverOptions(scale_anchor=True, scale_target=gt_t1_norm)).solve(problem)
    final = res.diagnostics["scale_anchor"]["final_t1_norm"]
    np.testing.assert_allclose(final, gt_t1_norm, rtol=1e-3)
