"""端到端求解：与独立 mpmath 高精度参考比较 + Vieta。

参考答案只来自 tests.conftest 的显式已知根或 polyroots.reference(mpmath)，
绝不调用被测内核自证。
"""

from __future__ import annotations

import numpy as np
import pytest

from polyroots import SolveOptions, solve_polynomial
from polyroots.models import KernelName, SolveStatus
from polyroots.reference import high_precision_roots, match_roots

pytestmark = pytest.mark.integration

KERNELS = [KernelName.COMPANION, KernelName.ABERTH]


def _desc(coeffs_asc: np.ndarray) -> list[list[float]]:
    return [[c.real, c.imag] for c in np.asarray(coeffs_asc)[::-1]]


@pytest.mark.parametrize("kernel", KERNELS)
def test_known_real_roots_recovered(kernel, real_roots_poly) -> None:
    roots, coeffs = real_roots_poly
    res = solve_polynomial(_desc(coeffs), options=SolveOptions(kernel=kernel),
                           run_id=f"real-{kernel.value}")
    assert res.status == SolveStatus.CONVERGED
    got = [r.value for r in res.roots]
    matches = match_roots(got, [complex(z) for z in roots])
    assert len(matches) == len(roots)
    assert max(d for _, _, d in matches) < 1e-9
    assert res.factor_error.max_rel_coeff_error < 1e-9
    assert res.vieta.sum_rel_error < 1e-9
    assert res.vieta.product_rel_error < 1e-9


@pytest.mark.parametrize("kernel", KERNELS)
def test_known_complex_roots_recovered_and_conjugate_paired(kernel, complex_roots_poly) -> None:
    roots, coeffs = complex_roots_poly
    res = solve_polynomial(_desc(coeffs), options=SolveOptions(kernel=kernel),
                           run_id=f"cplx-{kernel.value}")
    got = [r.value for r in res.roots]
    matches = match_roots(got, [complex(z) for z in roots])
    assert max(d for _, _, d in matches) < 1e-9
    # 两对共轭根：恰好 4 个根被两两配对，实根不配
    paired = {r.conjugate_of for r in res.roots if r.conjugate_of is not None}
    assert len(paired) == 4
    # 配对确实互为共轭
    for rec in res.roots:
        if rec.conjugate_of is not None:
            partner = res.roots[rec.conjugate_of].value
            assert abs(rec.value - np.conj(partner)) / (1 + abs(rec.value)) < 1e-8
    # 实根不配
    real_rec = [r for r in res.roots if abs(r.value.imag) < 1e-9]
    assert all(r.conjugate_of is None for r in real_rec)


@pytest.mark.parametrize("kernel", KERNELS)
def test_matches_independent_high_precision_reference(kernel, complex_roots_poly) -> None:
    _, coeffs = complex_roots_poly
    hp = high_precision_roots([complex(c) for c in coeffs], dps=50,
                              maxsteps=1000, extra_prec=50)
    assert hp.max_factor_error < 1e-40  # 参考自身必须可信
    res = solve_polynomial(_desc(coeffs), options=SolveOptions(kernel=kernel),
                           run_id=f"hp-{kernel.value}")
    matches = match_roots([r.value for r in res.roots], hp.roots)
    assert max(d for _, _, d in matches) < 1e-9


def test_roots_of_unity_high_degree_sparse(roots_of_unity_poly) -> None:
    roots, coeffs = roots_of_unity_poly
    n = len(roots)
    res = solve_polynomial(_desc(coeffs),
                           options=SolveOptions(kernel=KernelName.ABERTH,
                                                max_iterations=300),
                           run_id="unity64")
    assert res.status == SolveStatus.CONVERGED
    # 根本身必须精确到近机器精度（与显式单位根比较）
    matches = match_roots([r.value for r in res.roots], [complex(z) for z in roots])
    assert max(d for _, _, d in matches) < 1e-10
    # 关键：Vieta 与稳健重构判据必须通过，尽管 float64 严格重构被消去放大
    assert res.vieta.sum_rel_error < 1e-9
    assert res.factor_error.max_rel_coeff_error < 1e-9
    assert res.factor_error.strict_float64_error > 1e-3
    assert res.factor_error.high_precision_error is not None
    assert res.factor_error.high_precision_error < 1e-9
    # 必须有一条警告解释这个“看起来算错”的现象，而不是静默
    assert any("消去" in w for w in res.warnings)


def test_zero_root_polynomial(zero_roots_poly) -> None:
    roots, coeffs = zero_roots_poly
    res = solve_polynomial(_desc(coeffs), run_id="x5")
    assert res.degree == 5
    assert all(r.kind.value == "near_repeated" or abs(r.value) < 1e-12
               for r in res.roots)
    assert all(abs(r.value) < 1e-12 for r in res.roots)
    assert res.factor_error.max_rel_coeff_error < 1e-12


def test_wilkinson_polynomial_solver_error_is_witnessed_by_conditioning(
    wilkinson_like_poly,
) -> None:
    """Wilkinson 根 1..15：float64 下部分根误差约 1e-6（病态，非实现错误）。

    系统必须通过敏感性 κ 与高精度参考如实呈现这一误差量级，而不是用小残差
    宣称“精确”。
    """
    roots, coeffs = wilkinson_like_poly
    hp = high_precision_roots([complex(c) for c in coeffs], dps=50,
                              maxsteps=2000, extra_prec=50)
    res = solve_polynomial(_desc(coeffs), run_id="wilk15")
    assert res.status == SolveStatus.CONVERGED
    matches = match_roots([r.value for r in res.roots], hp.roots)
    worst = max(d for _, _, d in matches)
    # 与独立参考的偏差确实在 Wilkinson 病态量级，系统应能看到，而非掩盖
    assert worst > 1e-9
    assert worst < 1e-3
    # 至少存在高敏感性根，且残差远好于真实误差 —— 证明不能只看残差
    max_kappa = max(r.evidence.sensitivity_indicator for r in res.roots)
    assert max_kappa > 1e7
    best_res = min(r.evidence.relative_residual for r in res.roots)
    assert best_res < worst  # 残差显著小于真实根误差
