"""近重根专项：小残差 != 准确；迭代耗尽保留未收敛状态。"""

from __future__ import annotations
import numpy as np
import pytest
from polyroots import SolveOptions, solve_polynomial
from polyroots.models import KernelName, RootKind, SolveStatus
from polyroots.reference import high_precision_roots, match_roots
from tests.conftest import poly_from_roots

pytestmark = pytest.mark.integration


def _desc(coeffs_asc: np.ndarray) -> list[list[float]]:
    return [[c.real, c.imag] for c in np.asarray(coeffs_asc)[::-1]]


@pytest.mark.parametrize("kernel", [KernelName.COMPANION, KernelName.ABERTH])
def test_near_double_roots_labeled_despite_tiny_residual(kernel) -> None:
    roots = [1.0, 1.0 + 1e-7, -4.0, 0.5]
    coeffs = poly_from_roots(roots)
    res = solve_polynomial(_desc(coeffs), options=SolveOptions(kernel=kernel),
                           run_id=f"near-{kernel.value}")

    near = [r for r in res.roots if r.kind == RootKind.NEAR_REPEATED]
    assert len(near) == 2
    for rec in near:
        # 残差可以极小……
        assert rec.evidence.relative_residual < 1e-12
        # ……但簇间距很小且敏感性很大，系统必须如实标注，不能据此声称准确
        assert rec.evidence.cluster_separation < 1e-6
        assert rec.evidence.sensitivity_indicator > 1e6
    assert any("近重根" in w for w in res.warnings)


def test_near_double_float64_error_is_real_and_bounded_by_conditioning() -> None:
    """以高精度参考为真值，float64 求出的近重根确实只有 ~1e-9 精度，
    尽管残差 ~1e-16。这是本服务必须防止的核心“静默算错”场景。"""
    roots = [1.0, 1.0 + 1e-7, -4.0, 0.5]
    coeffs = poly_from_roots(roots)
    hp = high_precision_roots([complex(c) for c in coeffs], dps=60,
                              maxsteps=1000, extra_prec=60)
    res = solve_polynomial(_desc(coeffs),
                           options=SolveOptions(kernel=KernelName.COMPANION),
                           run_id="near-truth")
    matches = match_roots([r.value for r in res.roots], hp.roots)
    worst = max(d for _, _, d in matches)
    # 近重根求解误差比孤立根大很多数量级
    assert worst > 1e-11
    assert worst < 1e-7
    # 而对应根残差比真实误差小几个数量级 —— 若只看残差就会静默误判
    near = [r for r in res.roots if r.kind == RootKind.NEAR_REPEATED]
    assert min(r.evidence.relative_residual for r in near) < worst * 1e-3


def test_exact_double_roots_are_not_claimed_converged() -> None:
    coeffs = poly_from_roots([1.0, 1.0, 2.0, 2.0])  # (x-1)^2 (x-2)^2
    res = solve_polynomial(
        _desc(coeffs),
        options=SolveOptions(kernel=KernelName.ABERTH, max_iterations=300),
        run_id="exact-double",
    )
    # 精确重根无法在 float64 分离到 tol=1e-12：必须保留未收敛，而非假装成功
    assert res.status == SolveStatus.NOT_CONVERGED
    assert res.kernel.unconverged_count >= 1
    unconv = [r for r in res.roots if r.kind == RootKind.UNCONVERGED]
    assert len(unconv) >= 1
    # 未收敛根仍给出当前近似值与残差，状态可重放
    for rec in unconv:
        assert np.isfinite(rec.value)
        assert rec.evidence.iterations is not None and rec.evidence.iterations >= 1
        assert "未达到" in rec.evidence.note


def test_iteration_budget_exhaustion_preserves_state() -> None:
    roots = [1.0, 1.0 + 1e-9, -4.0, 0.5]
    coeffs = poly_from_roots(roots)
    res = solve_polynomial(
        _desc(coeffs),
        options=SolveOptions(kernel=KernelName.ABERTH,
                             convergence_tol=1e-13, max_iterations=8),
        run_id="budget-8",
    )
    assert res.status == SolveStatus.NOT_CONVERGED
    assert res.kernel.iterations_used <= 8
    assert res.kernel.unconverged_count >= 1
    assert len(res.roots) == 4  # 即便未收敛也保留全部根的当前状态
    # 中间状态被保留用于判断与重放
    assert "final_max_correction" in res.kernel.intermediate
    assert res.kernel.intermediate["final_max_correction"] > 1e-13
    assert any("未达到" in w for w in res.warnings)


def test_seeded_run_is_reproducible() -> None:
    roots = [1 + 1j, 2 - 3j, -1.0]
    coeffs = poly_from_roots(roots)
    opts = SolveOptions(kernel=KernelName.ABERTH, seed=42)
    r1 = solve_polynomial(_desc(coeffs), options=opts, run_id="seed-a")
    r2 = solve_polynomial(_desc(coeffs), options=opts, run_id="seed-b")
    a = np.array([r.value for r in r1.roots])
    b = np.array([r.value for r in r2.roots])
    np.testing.assert_array_equal(a, b)


def test_different_seed_can_change_path_but_not_validity() -> None:
    roots = [0.3, 1.7, -2.2, 4.1, -0.8]
    coeffs = poly_from_roots(roots)
    r = solve_polynomial(_desc(coeffs),
                         options=SolveOptions(kernel=KernelName.ABERTH, seed=7),
                         run_id="seed7")
    assert r.status == SolveStatus.CONVERGED
    matches = match_roots([x.value for x in r.roots], [complex(z) for z in roots])
    assert max(d for _, _, d in matches) < 1e-9
