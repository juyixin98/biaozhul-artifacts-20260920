"""次数边界与 auto 内核选择。"""

from __future__ import annotations

import pytest

from polyroots import SolveOptions, solve_polynomial
from polyroots.models import CoeffOrder, KernelName, SolveStatus

pytestmark = pytest.mark.unit


def test_constant_nonzero_polynomial_has_no_roots() -> None:
    res = solve_polynomial([5.0], run_id="const5")
    assert res.degree == 0
    assert res.status == SolveStatus.CONVERGED
    assert res.roots == []
    assert res.factor_error.max_rel_coeff_error < 1e-12


def test_linear_polynomial_exact_root_both_kernels() -> None:
    # 2x + 3（降序 [2,3]）-> x = -1.5
    for kernel in (KernelName.COMPANION, KernelName.ABERTH):
        res = solve_polynomial([2, 3], options=SolveOptions(kernel=kernel),
                               run_id=f"lin-{kernel.value}")
        assert len(res.roots) == 1
        assert res.roots[0].value == pytest.approx(-1.5 + 0j, abs=1e-12)


def test_auto_kernel_picks_companion_for_low_degree() -> None:
    coeffs = [1, 0, 0, -1]  # x^3 - 1，降序
    res = solve_polynomial(coeffs,
                           options=SolveOptions(kernel=KernelName.AUTO),
                           run_id="auto-low")
    assert res.kernel.name == "companion"


def test_auto_kernel_picks_aberth_for_high_degree() -> None:
    n = 120
    coeffs = [-1.0] + [0.0] * (n - 1) + [1.0]  # x^120 - 1，升序
    res = solve_polynomial(
        coeffs, order=CoeffOrder.ASCENDING,
        options=SolveOptions(kernel=KernelName.AUTO, max_degree=256,
                             max_iterations=500),
        run_id="auto-high",
    )
    assert res.kernel.name == "aberth"
    assert res.status == SolveStatus.CONVERGED
    assert len(res.roots) == n


def test_leading_zero_stripping_changes_reported_degree() -> None:
    # 降序 [0,0,1,2]：真实是 x+2
    res = solve_polynomial([0, 0, 1, 2], run_id="strip2")
    assert res.degree == 1
    assert len(res.roots) == 1
    assert res.roots[0].value == pytest.approx(-2.0 + 0j, abs=1e-12)
    assert any("剥离" in w for w in res.warnings)
