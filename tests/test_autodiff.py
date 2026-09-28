"""Core AD tests with concrete analytic assertions (no HTTP)."""

from __future__ import annotations

import numpy as np
import pytest

from hvpsvc import autodiff as ad
from hvpsvc.errors import (
    ComputeFailureError,
    InputError,
    NonSmoothError,
    ResourceExhaustedError,
)
from hvpsvc.graph import Budget, NonsmoothConfig
from hvpsvc.state import StateStore

from . import fixtures as fx


def _make(spec, point, nonsmooth=None, limits=None):
    store = StateStore()
    st = store.create(spec, nonsmooth=nonsmooth, budget_limits=limits)
    st.set_point(point)
    return st


# --------------------------------------------------------------------------
# Analytic quadratic
# --------------------------------------------------------------------------

def test_quadratic_value_and_gradient_analytic():
    st = _make(fx.quadratic3_spec(), {"x": fx.X_QUAD3.tolist()})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    x = fx.X_QUAD3
    expected_value = float(x @ fx.H_QUAD3 @ x) / 2.0
    assert gg.primal_value == pytest.approx(expected_value, rel=1e-12)
    np.testing.assert_allclose(grad, fx.H_QUAD3 @ x, rtol=1e-12, atol=1e-12)


def test_quadratic_hvp_analytic():
    st = _make(fx.quadratic3_spec(), {"x": fx.X_QUAD3.tolist()})
    gg = st.gradient_graph(st.new_budget())
    hv = ad.hvp(gg, st.point, fx.V_QUAD3, st.layout, st.new_budget(),
                st.nonsmooth)
    np.testing.assert_allclose(hv, fx.H_QUAD3 @ fx.V_QUAD3,
                               rtol=1e-12, atol=1e-12)


def test_hvp_is_linear_in_v():
    st = _make(fx.quadratic3_spec(), {"x": fx.X_QUAD3.tolist()})
    gg = st.gradient_graph(st.new_budget())
    rng = np.random.default_rng(1)
    a, b = rng.standard_normal(3), rng.standard_normal(3)
    c = 0.7
    hv_sum = ad.hvp(gg, st.point, a + c * b, st.layout, st.new_budget(),
                    st.nonsmooth)
    hv_a = ad.hvp(gg, st.point, a, st.layout, st.new_budget(), st.nonsmooth)
    hv_b = ad.hvp(gg, st.point, b, st.layout, st.new_budget(), st.nonsmooth)
    np.testing.assert_allclose(hv_sum, hv_a + c * hv_b, atol=1e-11)


def test_zero_direction_is_exact_zero():
    st = _make(fx.quadratic3_spec(), {"x": fx.X_QUAD3.tolist()})
    gg = st.gradient_graph(st.new_budget())
    hv = ad.hvp(gg, st.point, np.zeros(3), st.layout, st.new_budget(),
                st.nonsmooth)
    assert np.count_nonzero(hv) == 0


# --------------------------------------------------------------------------
# Shared subgraph: node y used twice, must be accumulated correctly
# --------------------------------------------------------------------------

def test_shared_subgraph_gradient_and_hvp_analytic():
    st = _make(fx.shared_subgraph_spec(), {"x": fx.X_SHARED.tolist()})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    x = fx.X_SHARED
    # f = y^2+y, y=x0*x1 -> grad = ((2y+1)*x1, (2y+1)*x0)
    y = x[0] * x[1]
    np.testing.assert_allclose(grad, [(2 * y + 1) * x[1], (2 * y + 1) * x[0]],
                               atol=1e-12)
    v = np.array([2.5, -1.5])
    hv = ad.hvp(gg, st.point, v, st.layout, st.new_budget(), st.nonsmooth)
    np.testing.assert_allclose(hv, fx.H_SHARED @ v, rtol=1e-12, atol=1e-12)


def test_shared_node_is_built_once_in_gradient_graph():
    st = _make(fx.shared_subgraph_spec(), {"x": fx.X_SHARED.tolist()})
    gg = st.gradient_graph(st.new_budget())
    # the mirrored value of shared node y exists exactly once
    y_val_nodes = [n for n in gg.expr.order if n == "val:y"]
    assert len(y_val_nodes) == 1
    # y feeds mul(y,y) twice and add(y2,y) once: exactly 3 adjoint
    # contributions - neither dropped (under-count) nor duplicated.
    assert gg.contributions["y"] == 3
    # each leaf x0/x1 has a single consumer (the mul node y)
    from hvpsvc.graph import leaf_id
    assert gg.contributions[leaf_id("x", 0)] == 1
    assert gg.contributions[leaf_id("x", 1)] == 1
    # the three contributions combine through exactly two adjoint add nodes
    # (excluding the mirrored primal add node "val:f")
    add_nodes = [n for n in gg.expr.order
                 if gg.expr.nodes[n].op == "add"
                 and not n.startswith("val:")]
    assert len(add_nodes) == 2


# --------------------------------------------------------------------------
# Scalar variable + two-variable parameter sharing
# --------------------------------------------------------------------------

def test_scalar_variable_square():
    st = _make(fx.scalar_square_spec(), {"s": 3.0})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    assert grad.shape == (1,)
    assert grad[0] == pytest.approx(7.0, abs=1e-12)   # 2s+1 at s=3
    hv = ad.hvp(gg, st.point, np.array([1.0]), st.layout, st.new_budget(),
                st.nonsmooth)
    assert hv[0] == pytest.approx(2.0, abs=1e-12)


def test_two_variables_layout_binding():
    st = _make(fx.two_variable_spec(), {"x": [1.5], "y": -0.8})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    # H = [[2,-1],[-1,2]]; point (x0,y) = (1.5,-0.8)
    np.testing.assert_allclose(grad, [2 * 1.5 - (-0.8), -1.5 + 2 * (-0.8)],
                               atol=1e-12)
    v = st.layout.bind_vector({"x": [2.0], "y": -1.0}, what="vector")
    np.testing.assert_allclose(v, [2.0, -1.0])
    hv = ad.hvp(gg, st.point, v, st.layout, st.new_budget(), st.nonsmooth)
    np.testing.assert_allclose(hv, np.array([[2, -1], [-1, 2]]) @ v,
                               atol=1e-12)

    with pytest.raises(InputError) as ei:
        st.layout.bind_vector({"x": [2.0, 3.0], "y": -1.0}, what="vector")
    assert ei.value.category.value == "input_error"
    with pytest.raises(InputError):
        st.layout.bind_vector({"x": [2.0]}, what="vector")  # missing y


# --------------------------------------------------------------------------
# Non-smooth points
# --------------------------------------------------------------------------

def test_abs_kink_rejected_by_default_with_explicit_detail():
    st = _make(fx.nonsmooth_spec(), {"z": [0.0, 1.0]})
    with pytest.raises(NonSmoothError) as ei:
        st.gradient_graph(st.new_budget())
    assert ei.value.category.value == "nonsmooth_point"
    assert ei.value.detail["op"] == "abs"
    assert ei.value.detail["inputs"] == [0.0]


def test_max_tie_rejected():
    st = _make(fx.nonsmooth_spec(), {"z": [2.0, 2.0]})
    with pytest.raises(NonSmoothError) as ei:
        st.gradient_graph(st.new_budget())
    assert ei.value.detail["op"] == "max"
    assert ei.value.detail["inputs"] == [2.0, 2.0]


def test_designated_subgradient_used_at_kink():
    cfg = NonsmoothConfig(policy="subgradient", subgradient=0.25)
    st = _make(fx.nonsmooth_spec(), {"z": [0.0, 0.0]}, nonsmooth=cfg)
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    # |z0|: 2*.25-1 = -0.5 ; relu(z1): .25 ; max tie: (.25, .75)
    np.testing.assert_allclose(grad, [-0.5 + 0.25, 0.25 + 0.75], atol=1e-12)


def test_kink_away_from_point_is_smooth_branch():
    st = _make(fx.nonsmooth_spec(), {"z": [-0.5, 0.2]})
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    # abs(-.5) -> -1 ; relu(.2) -> 1 ; max(-.5,.2) -> (0,1)
    np.testing.assert_allclose(grad, [-1.0, 2.0], atol=1e-12)


def test_invalid_subgradient_config_rejected():
    with pytest.raises(InputError):
        NonsmoothConfig(policy="subgradient", subgradient=1.5)
    with pytest.raises(InputError):
        NonsmoothConfig(policy="bogus")


# --------------------------------------------------------------------------
# Overflow / domain / budgets
# --------------------------------------------------------------------------

def test_log_domain_error_is_compute_failure():
    st = _make(fx.log_domain_spec(), {"x": -1.0})
    with pytest.raises(ComputeFailureError) as ei:
        st.gradient_graph(st.new_budget())
    assert ei.value.category.value == "compute_failure"
    assert "log" in ei.value.detail["op"]


def test_exp_overflow_is_compute_failure():
    st = _make(fx.exp_spec(), {"x": 1000.0})
    with pytest.raises(ComputeFailureError) as ei:
        st.gradient_graph(st.new_budget())
    assert ei.value.category.value == "compute_failure"


def test_hvp_overflow_diagnosed():
    # primal exp(x) is finite at x=700 (~1e304), but the HVP tangent
    # exp(x)*v overflows for a large v -> diagnosed during the hvp stage
    st = _make(fx.exp_spec(), {"x": 700.0})
    gg = st.gradient_graph(st.new_budget())
    with pytest.raises(ComputeFailureError) as ei:
        ad.hvp(gg, st.point, np.array([1e200]), st.layout,
               Budget(max_evals=10_000), st.nonsmooth)
    assert ei.value.detail["stage"] == "hvp"


def test_node_budget_exceeded_at_parse():
    spec = fx.quadratic3_spec()
    with pytest.raises(ResourceExhaustedError) as ei:
        StateStore().create(spec, budget_limits={"max_nodes": 3})
    assert ei.value.category.value == "resource_exhausted"
    assert ei.value.detail["limit"] == "max_nodes"


def test_eval_budget_exceeded_at_hvp():
    st = _make(fx.quadratic3_spec(), {"x": fx.X_QUAD3.tolist()})
    gg = st.gradient_graph(st.new_budget())
    tight = Budget(max_evals=2)
    with pytest.raises(ResourceExhaustedError) as ei:
        ad.hvp(gg, st.point, fx.V_QUAD3, st.layout, tight, st.nonsmooth)
    assert ei.value.detail["limit"] == "max_evals"


def test_budget_accounts_every_node():
    b = Budget(max_evals=10)
    b.tick_eval(10)  # exactly at the limit is allowed
    with pytest.raises(ResourceExhaustedError):
        b.tick_eval()


# --------------------------------------------------------------------------
# Parse/input errors
# --------------------------------------------------------------------------

def test_unknown_op_is_input_error():
    spec = {
        "variables": [{"name": "x", "shape": []}],
        "expression": {"nodes": [{"id": "n", "op": "frobnicate",
                                  "args": ["x"]}], "output": "n"},
    }
    with pytest.raises(InputError) as ei:
        StateStore().create(spec)
    assert "known_ops" in ei.value.detail


def test_tensor_used_without_index_is_input_error():
    spec = {
        "variables": [{"name": "x", "shape": [2]}],
        "expression": {"nodes": [{"id": "n", "op": "neg", "args": ["x"]}],
                       "output": "n"},
    }
    with pytest.raises(InputError) as ei:
        StateStore().create(spec)
    assert "get" in str(ei.value)


def test_index_out_of_range_is_input_error():
    spec = {
        "variables": [{"name": "x", "shape": [2]}],
        "expression": {"nodes": [{"id": "xi", "op": "get", "args": ["x"],
                                  "index": 2}], "output": "xi"},
    }
    with pytest.raises(InputError) as ei:
        StateStore().create(spec)
    assert ei.value.detail["index"] == 2


def test_duplicate_node_and_unknown_arg():
    base = fx.quadratic3_spec()
    bad = {"variables": base["variables"],
           "expression": {"nodes": base["expression"]["nodes"][:2] + [
               {"id": "x0", "op": "neg", "args": ["x0"]}],
               "output": "x0"}}
    with pytest.raises(InputError):
        StateStore().create(bad)


def test_shape_mismatch_on_point():
    st = StateStore().create(fx.quadratic3_spec())
    with pytest.raises(InputError) as ei:
        st.set_point({"x": [1.0, 2.0]})
    assert ei.value.category.value == "input_error"


def test_vector_shape_must_bind_to_layout():
    st = _make(fx.quadratic3_spec(), {"x": fx.X_QUAD3.tolist()})
    gg = st.gradient_graph(st.new_budget())
    with pytest.raises(InputError) as ei:
        ad.hvp(gg, st.point, np.array([1.0, 2.0]), st.layout,
               st.new_budget(), st.nonsmooth)
    assert "layout" in str(ei.value)
