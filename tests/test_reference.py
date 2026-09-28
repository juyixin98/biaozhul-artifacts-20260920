"""Independent verification tests.

References are generated OUTSIDE the autodiff core:

- hand-derived analytic constants in fixtures.py;
- an independent mpmath (50-digit) interpreter + explicit dense Hessian
  in hvpsvc/validation.py;
- central finite differences of the primal value.
"""

from __future__ import annotations

import numpy as np

from hvpsvc import autodiff as ad
from hvpsvc.state import StateStore
from hvpsvc.validation import (
    MpmathReference,
    check_gradient_against_ref,
    check_vector_against_ref,
    finite_difference_gradient,
)

from . import fixtures as fx


def _state(spec, point):
    st = StateStore().create(spec)
    st.set_point(point)
    return st


def test_mpmath_reference_hessian_matches_analytic_quadratic():
    """The independent oracle itself must reproduce the hand-derived H."""
    st = _state(fx.quadratic3_spec(), {"x": fx.X_QUAD3.tolist()})
    ref = MpmathReference(st.spec, st.layout)
    _, grad_ref, H = ref.value_gradient_hessian(st.point)
    np.testing.assert_allclose(H, fx.H_QUAD3, atol=1e-20)
    np.testing.assert_allclose(grad_ref, fx.H_QUAD3 @ fx.X_QUAD3, atol=1e-20)


def test_service_hvp_matches_explicit_high_precision_hessian_nonlinear():
    st = _state(fx.nonlinear_spec(), {"x": fx.X_NONLINEAR.tolist()})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    ref = MpmathReference(st.spec, st.layout)
    _, grad_ref, H = ref.value_gradient_hessian(st.point)

    assert check_gradient_against_ref(grad_ref, grad, tol=1e-9).passed

    for v in [np.array([1.0, 0.0, 0.0]),
              np.array([0.0, 1.0, 0.0]),
              np.array([1.3, -2.7, 0.4]),
              np.zeros(3)]:
        hv = ad.hvp(gg, st.point, v, st.layout, st.new_budget(),
                    st.nonsmooth)
        res = check_vector_against_ref(
            H @ v if v.any() else np.zeros(3), hv, tol=1e-9)
        assert res.passed, res.reason


def test_hvp_columns_assemble_full_hessian_nonlinear():
    """Every HVP column H e_i agrees, column by column, with explicit H."""
    st = _state(fx.nonlinear_spec(), {"x": fx.X_NONLINEAR.tolist()})
    gg = st.gradient_graph(st.new_budget())
    _, _, H = MpmathReference(st.spec, st.layout).value_gradient_hessian(
        st.point)
    n = st.layout.total_size
    assembled = np.zeros((n, n))
    for i in range(n):
        e = np.zeros(n)
        e[i] = 1.0
        assembled[:, i] = ad.hvp(gg, st.point, e, st.layout,
                                 st.new_budget(), st.nonsmooth)
    np.testing.assert_allclose(assembled, H, rtol=1e-9, atol=1e-10)
    # symmetry is a real property check, not just cosmetic
    np.testing.assert_allclose(assembled, assembled.T, atol=1e-12)


def test_central_differences_confirm_gradient_nonlinear():
    st = _state(fx.nonlinear_spec(), {"x": fx.X_NONLINEAR.tolist()})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())

    def primal(xx):
        return float(st.graph.evaluate(
            [st.output], xx, st.layout,
            budget=st.new_budget(), stage="forward")[0])

    fd = finite_difference_gradient(primal, st.point, step=1e-6)
    res = check_gradient_against_ref(fd, grad, tol=1e-6,
                                     name="fd")
    assert res.passed, res.reason


def test_shared_subgraph_against_independent_reference():
    st = _state(fx.shared_subgraph_spec(), {"x": fx.X_SHARED.tolist()})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    _, grad_ref, H = MpmathReference(st.spec, st.layout) \
        .value_gradient_hessian(st.point)
    np.testing.assert_allclose(grad_ref, grad, atol=1e-12)
    np.testing.assert_allclose(H, fx.H_SHARED, atol=1e-20)
    v = np.array([3.1, 0.7])
    hv = ad.hvp(gg, st.point, v, st.layout, st.new_budget(), st.nonsmooth)
    np.testing.assert_allclose(hv, H @ v, atol=1e-12)


def test_two_variable_reference_and_scalar_binding():
    st = _state(fx.two_variable_spec(), {"x": [1.5], "y": -0.8})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    _, grad_ref, H = MpmathReference(st.spec, st.layout) \
        .value_gradient_hessian(st.point)
    np.testing.assert_allclose(grad, grad_ref, atol=1e-14)
    assert H.shape == (2, 2)
    v = np.array([-1.0, 2.0])
    hv = ad.hvp(gg, st.point, v, st.layout, st.new_budget(), st.nonsmooth)
    np.testing.assert_allclose(hv, H @ v, atol=1e-12)


def test_reference_rejects_oversize_dimension():
    import pytest

    from hvpsvc.errors import InputError
    from hvpsvc.tensor import InputLayout
    big = {"variables": [{"name": "x", "shape": [65]}],
           "expression": {"nodes": [], "output": "x"}}
    layout = InputLayout.from_specs(big["variables"])
    with pytest.raises(InputError) as ei:
        MpmathReference(big, layout)
    assert "64" in str(ei.value)
