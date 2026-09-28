"""Gradient correctness against analytic and high-precision references."""

from __future__ import annotations

import numpy as np

from hvp_service.service import HvpRequestData

from .conftest import make_graph, nonlinear_spec, quadratic_spec, sharing_spec
from .references import mp_grad, nl_grad, nl_value_mp, quad_grad, sharing_value_mp


def _grad(service, spec, point):
    nodes, output = spec()
    gid = make_graph(service, nodes, output)
    result = service.hvp(gid, HvpRequestData(point=point.tolist(), vector=np.zeros_like(point).tolist()))
    return np.array(result.gradient)


def test_quadratic_gradient_matches_analytic(service, quad_point) -> None:
    grad = _grad(service, quadratic_spec, quad_point)
    np.testing.assert_allclose(grad, quad_grad(quad_point), atol=1e-12)


def test_nonlinear_gradient_matches_analytic(service, nl_point) -> None:
    grad = _grad(service, nonlinear_spec, nl_point)
    np.testing.assert_allclose(grad, nl_grad(nl_point), atol=1e-12)


def test_nonlinear_gradient_matches_mpmath(service, nl_point) -> None:
    grad = _grad(service, nonlinear_spec, nl_point)
    np.testing.assert_allclose(grad, mp_grad(nl_value_mp, nl_point), atol=1e-9)


def test_shared_subgraph_gradient_matches_mpmath(service) -> None:
    """z = x0*x1 + sin(x0); f = z^2 + z — z feeds two consumers."""
    point = np.array([0.9, 1.4])
    grad = _grad(service, sharing_spec, point)
    np.testing.assert_allclose(grad, mp_grad(sharing_value_mp, point), atol=1e-9)
