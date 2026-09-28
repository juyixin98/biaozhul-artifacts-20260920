"""HVP correctness: analytic Hessians, explicit high-precision Hessian,
zero direction, linearity in the direction, and gradient-difference
consistency."""

from __future__ import annotations

import numpy as np
import pytest

from hvp_service.service import HvpRequestData

from .conftest import make_graph, nonlinear_spec, quadratic_spec, sharing_spec
from .references import (
    mp_hessian,
    nl_hessian,
    nl_value_mp,
    quad_hvp,
    quad_value,
    sharing_value_mp,
)


def _hvp(service, spec, point, vector):
    nodes, output = spec()
    gid = make_graph(service, nodes, output)
    return service.hvp(gid, HvpRequestData(point=point.tolist(), vector=vector.tolist()))


def test_quadratic_hvp_matches_analytic(service, quad_point, quad_direction) -> None:
    result = _hvp(service, quadratic_spec, quad_point, quad_direction)
    np.testing.assert_allclose(np.array(result.hvp), quad_hvp(quad_direction), atol=1e-12)
    assert result.value == pytest.approx(quad_value(quad_point), abs=1e-12)


def test_nonlinear_hvp_matches_analytic_hessian(service, nl_point, nl_direction) -> None:
    result = _hvp(service, nonlinear_spec, nl_point, nl_direction)
    expected = nl_hessian(nl_point) @ nl_direction
    np.testing.assert_allclose(np.array(result.hvp), expected, atol=1e-10)


def test_nonlinear_hvp_matches_mpmath_hessian(service, nl_point, nl_direction) -> None:
    """Explicit high-precision Hessian (mpmath, 50 digits) as the oracle."""
    result = _hvp(service, nonlinear_spec, nl_point, nl_direction)
    H = mp_hessian(nl_value_mp, nl_point)
    np.testing.assert_allclose(np.array(result.hvp), H @ nl_direction, atol=1e-8)


def test_shared_subgraph_hvp_matches_mpmath_hessian(service) -> None:
    point = np.array([0.9, 1.4])
    direction = np.array([0.6, -1.1])
    result = _hvp(service, sharing_spec, point, direction)
    H = mp_hessian(sharing_value_mp, point)
    np.testing.assert_allclose(np.array(result.hvp), H @ direction, atol=1e-8)


def test_zero_direction_gives_zero_hvp_but_real_gradient(service, nl_point) -> None:
    zero = np.zeros_like(nl_point)
    result = _hvp(service, nonlinear_spec, nl_point, zero)
    np.testing.assert_array_equal(np.array(result.hvp), np.zeros_like(nl_point))
    assert np.linalg.norm(np.array(result.gradient)) > 0.1  # gradient still computed


def test_hvp_is_linear_in_direction(service, nl_point, nl_direction) -> None:
    other = np.array([0.3, 0.9])
    a, b = 1.7, -0.4
    combined = _hvp(service, nonlinear_spec, nl_point, a * nl_direction + b * other)
    first = _hvp(service, nonlinear_spec, nl_point, nl_direction)
    second = _hvp(service, nonlinear_spec, nl_point, other)
    np.testing.assert_allclose(
        np.array(combined.hvp),
        a * np.array(first.hvp) + b * np.array(second.hvp),
        atol=1e-10,
    )


def test_hvp_matches_central_difference_of_service_gradient(service, nl_point, nl_direction) -> None:
    """Consistency check: HVP ~= (grad(x+eps v) - grad(x-eps v)) / (2 eps)."""
    eps = 1e-5
    plus = _hvp(service, nonlinear_spec, nl_point + eps * nl_direction, np.zeros(2))
    minus = _hvp(service, nonlinear_spec, nl_point - eps * nl_direction, np.zeros(2))
    fd = (np.array(plus.gradient) - np.array(minus.gradient)) / (2 * eps)
    exact = _hvp(service, nonlinear_spec, nl_point, nl_direction)
    np.testing.assert_allclose(np.array(exact.hvp), fd, atol=1e-6)


def test_hvp_diagnostics_carry_run_id_and_counts(service, nl_point, nl_direction) -> None:
    result = _hvp(service, nonlinear_spec, nl_point, nl_direction)
    diag = result.diagnostics
    assert result.run_id and diag["run_id"] == result.run_id
    assert diag["forward_nodes"] == 9
    assert diag["gradient_nodes"] > 0
    assert diag["total_nodes"] > diag["forward_nodes"] + diag["gradient_nodes"]
    assert diag["nodes_evaluated"] == diag["total_nodes"]
    assert diag["kinks"] == []
