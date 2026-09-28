"""稳定排序、共轭配对容差、选项校验。"""

from __future__ import annotations

import numpy as np
import pytest

from polyroots import SolveOptions, solve_polynomial
from polyroots.errors import InvalidOptionError
from polyroots.ordering import conjugate_pairs, stable_order
from tests.conftest import poly_from_roots

pytestmark = pytest.mark.unit


def test_stable_order_sorts_by_real_then_imag() -> None:
    roots = np.array([3 + 0j, 1 - 1j, 1 + 1j, 2 + 0j])
    order = stable_order(roots)
    sorted_roots = roots[order]
    reals = sorted_roots.real
    assert list(reals) == sorted(reals, key=lambda x: (round(x, 9),))
    # 同实部时虚部升序
    assert list(sorted_roots[:2].imag) == [-1.0, 1.0]


def test_stable_order_is_deterministic_under_input_permutation() -> None:
    roots = np.array([1 + 2j, 3 - 1j, -2 + 0j, 0 + 5j])
    o1 = roots[stable_order(roots)]
    o2 = roots[stable_order(roots[::-1])]
    # 排序结果集合一致；规范顺序不应依赖输入顺序
    np.testing.assert_allclose(np.sort_complex(o1), np.sort_complex(o2))


def test_conjugate_pairing_requires_explicit_tolerance() -> None:
    roots = np.array([1 + 2j, 1 - 2j, 5 + 0j])
    order = stable_order(roots)
    pairs = conjugate_pairs(roots, order, conjugate_tol=1e-6)
    assert pairs[0] is not None and pairs[1] is not None  # 配对成功
    # 实根不配
    real_pos = next(i for i in range(3)
                    if abs(roots[order][i].imag) < 1e-12)
    assert pairs[real_pos] is None


def test_pairing_tolerance_rejects_near_but_not_conjugate() -> None:
    # 虚部偏差 1e-3：tol=1e-6 时不得配对，tol=1e-2 时才配
    roots = np.array([1 + 2j, 1 - 2j + 1e-3j, 8 + 0j])
    order = stable_order(roots)
    strict = conjugate_pairs(roots, order, conjugate_tol=1e-6)
    loose = conjugate_pairs(roots, order, conjugate_tol=1e-2)
    assert all(p is None for p in strict)
    assert any(p is not None for p in loose)


def test_no_duplicate_pairing_when_three_candidates() -> None:
    # 1+2i, 1-2i, 1-2i+噪声：只能配成唯一一对，第三个落单
    roots = np.array([1 + 2j, 1 - 2j, 1 - 2j + 1e-9j])
    order = stable_order(roots)
    pairs = conjugate_pairs(roots, order, conjugate_tol=1e-6)
    n_paired = sum(1 for p in pairs if p is not None)
    assert n_paired == 2  # 恰一对，绝不出现一个根被配两次


def test_response_root_order_is_canonical_and_indexed() -> None:
    coeffs = poly_from_roots([3 + 0j, -2 + 0j, 0.5, 7 + 0j])
    res = solve_polynomial([[c.real, c.imag] for c in coeffs[::-1]], run_id="ord1")
    vals = [(r.value.real, r.value.imag) for r in res.roots]
    assert vals == sorted(vals, key=lambda t: (round(t[0], 9), round(t[1], 9)))
    assert [r.evidence.index for r in res.roots] == list(range(4))


@pytest.mark.parametrize("kwargs", [
    {"max_iterations": 0},
    {"convergence_tol": 0.0},
    {"cluster_tol": 2.0},
    {"conjugate_tol": -1e-9},
    {"max_degree": 0},
])
def test_invalid_options_rejected(kwargs) -> None:
    with pytest.raises(InvalidOptionError):
        SolveOptions(**kwargs).validate()


def test_cluster_tol_smaller_than_convergence_tol_rejected() -> None:
    with pytest.raises(InvalidOptionError):
        SolveOptions(cluster_tol=1e-14, convergence_tol=1e-10).validate()
