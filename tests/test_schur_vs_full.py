"""验收：Schur 补消元解 与 完整正规方程解 的一致性。"""

import numpy as np

from ba.simulate import scene_to_problem, simulate_scene
from ba.solver import BASolver, SolverOptions


def _scene():
    scene = simulate_scene(n_cameras=5, n_points=30, noise_px=1.0, seed=1, n_behind_camera=2)
    return scene_to_problem(scene)


def test_single_linearized_step_matches():
    """在同一点、同一阻尼下，两种求解方式给出的 LM 增量应一致。"""
    problem = _scene()
    solver = BASolver(SolverOptions())
    free_cams = list(range(1, len(problem.cameras)))

    # 先设尺度锚定目标，再在同一点、同一阻尼下分别求解
    solver._scale_target0 = float(np.linalg.norm(problem.cameras[1].t))
    ls, cost, n_neg, _ = solver._build_normal_equations(problem, free_cams)
    d_schur = BASolver._solve_schur(ls, damping=1e-3)
    d_full = BASolver._solve_full(ls, damping=1e-3)

    assert n_neg >= 1  # 场景含负深度观测
    np.testing.assert_allclose(d_schur, d_full, rtol=1e-8, atol=1e-10)


def test_full_optimization_matches():
    """完整 LM 优化后，两种方式收敛到相同的代价与结果。"""
    res_schur = BASolver(SolverOptions(solver="schur", max_iterations=50)).solve(_scene())
    res_full = BASolver(SolverOptions(solver="full", max_iterations=50)).solve(_scene())

    assert res_schur.converged and res_full.converged
    np.testing.assert_allclose(res_schur.cost_history[-1], res_full.cost_history[-1], rtol=1e-9)
    for cs, cf in zip(res_schur.problem.cameras, res_full.problem.cameras):
        np.testing.assert_allclose(cs.t, cf.t, atol=1e-7)
        np.testing.assert_allclose(cs.R, cf.R, atol=1e-9)
    np.testing.assert_allclose(res_schur.problem.points, res_full.problem.points, atol=1e-6)


def test_schur_reduces_cost():
    res = BASolver(SolverOptions(solver="schur")).solve(_scene())
    assert res.cost_history[0] > res.cost_history[-1]
    # 每观测均方误差应接近噪声量级（1 像素噪声 -> ~1 px^2/obs）
    n_obs = res.diagnostics["num_observations"]
    rmse = np.sqrt(2 * res.cost_history[-1] / n_obs)
    assert rmse < 2.0
