"""Non-smooth points: reject policy, explicit subgradient policy, kink diagnostics."""

from __future__ import annotations

import numpy as np
import pytest

from hvp_service.errors import ErrorCategory, ServiceError
from hvp_service.service import HvpRequestData

from .conftest import make_graph, unary_spec


def test_abs_at_kink_rejected_by_default(service) -> None:
    nodes, output = unary_spec("abs")
    gid = make_graph(service, nodes, output)
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(point=[1.5, 0.0], vector=[1.0, 1.0]))
    err = exc.value
    assert err.category is ErrorCategory.NONSMOOTH_POINT
    assert err.run_id  # replayable
    assert err.details["op"] == "abs"
    assert err.details["node_id"] == 1
    assert err.details["kinks"][0]["index"] == [1]


def test_abs_at_kink_with_subgradient_policy(service) -> None:
    nodes, output = unary_spec("abs")
    gid = make_graph(service, nodes, output)
    result = service.hvp(
        gid,
        HvpRequestData(
            point=[1.5, 0.0],
            vector=[1.0, -2.0],
            nonsmooth_policy="subgradient",
            subgradient=0.5,
        ),
    )
    # grad of sum(abs) at [1.5, 0] with subgradient 0.5 at the kink
    np.testing.assert_allclose(result.gradient, [1.0, 0.5], atol=1e-12)
    # locally the chosen subgradient is constant -> zero curvature
    np.testing.assert_allclose(result.hvp, [0.0, 0.0], atol=1e-12)
    assert result.value == pytest.approx(1.5, abs=1e-12)
    assert len(result.diagnostics["kinks"]) == 1
    assert result.diagnostics["kinks"][0]["op"] == "abs"


def test_relu_at_kink_with_subgradient_policy(service) -> None:
    nodes, output = unary_spec("relu")
    gid = make_graph(service, nodes, output)
    result = service.hvp(
        gid,
        HvpRequestData(
            point=[-2.0, 0.0],
            vector=[1.0, 1.0],
            nonsmooth_policy="subgradient",
            subgradient=0.3,
        ),
    )
    np.testing.assert_allclose(result.gradient, [0.0, 0.3], atol=1e-12)
    np.testing.assert_allclose(result.hvp, [0.0, 0.0], atol=1e-12)


def test_abs_away_from_kink_behaves_normally(service) -> None:
    nodes, output = unary_spec("abs")
    gid = make_graph(service, nodes, output)
    result = service.hvp(gid, HvpRequestData(point=[-1.5, 2.0], vector=[3.0, 4.0]))
    np.testing.assert_allclose(result.gradient, [-1.0, 1.0], atol=1e-12)
    np.testing.assert_allclose(result.hvp, [0.0, 0.0], atol=1e-12)
    assert result.diagnostics["kinks"] == []


def test_subgradient_out_of_range_is_input_error(service) -> None:
    nodes, output = unary_spec("abs")
    gid = make_graph(service, nodes, output)
    with pytest.raises(ServiceError) as exc:
        service.hvp(
            gid,
            HvpRequestData(
                point=[1.0, 0.0],
                vector=[1.0, 1.0],
                nonsmooth_policy="subgradient",
                subgradient=1.5,
            ),
        )
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION
