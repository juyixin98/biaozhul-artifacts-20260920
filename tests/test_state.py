"""Training state: stored points, optimistic versioning, conflicts."""

from __future__ import annotations

import numpy as np
import pytest

from hvp_service.errors import ErrorCategory, ServiceError
from hvp_service.service import HvpRequestData

from .conftest import make_graph, quadratic_spec
from .references import quad_grad, quad_hvp


def test_set_point_bumps_version(service, quad_point, quad_direction) -> None:
    nodes, output = quadratic_spec()
    gid = make_graph(service, nodes, output)
    assert service.set_point(gid, quad_point.tolist()) == 1
    assert service.set_point(gid, quad_point.tolist()) == 2


def test_hvp_with_stored_point_and_matching_version(service, quad_point, quad_direction) -> None:
    nodes, output = quadratic_spec()
    gid = make_graph(service, nodes, output)
    version = service.set_point(gid, quad_point.tolist())
    result = service.hvp(
        gid,
        HvpRequestData(
            vector=quad_direction.tolist(),
            use_stored_point=True,
            expected_version=version,
        ),
    )
    np.testing.assert_allclose(np.array(result.hvp), quad_hvp(quad_direction), atol=1e-12)
    np.testing.assert_allclose(np.array(result.gradient), quad_grad(quad_point), atol=1e-12)
    assert result.diagnostics["point_version"] == version


def test_stale_expected_version_is_state_conflict(service, quad_point, quad_direction) -> None:
    nodes, output = quadratic_spec()
    gid = make_graph(service, nodes, output)
    service.set_point(gid, quad_point.tolist())
    service.set_point(gid, (quad_point + 0.1).tolist())  # training moved on
    with pytest.raises(ServiceError) as exc:
        service.hvp(
            gid,
            HvpRequestData(
                vector=quad_direction.tolist(),
                use_stored_point=True,
                expected_version=1,
            ),
        )
    err = exc.value
    assert err.category is ErrorCategory.STATE_CONFLICT
    assert err.details["expected_version"] == 1
    assert err.details["actual_version"] == 2


def test_stored_point_missing_is_state_conflict(service, quad_direction) -> None:
    nodes, output = quadratic_spec()
    gid = make_graph(service, nodes, output)
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(vector=quad_direction.tolist(), use_stored_point=True))
    assert exc.value.category is ErrorCategory.STATE_CONFLICT


def test_unknown_and_deleted_graph_is_not_found(service, quad_point, quad_direction) -> None:
    nodes, output = quadratic_spec()
    gid = make_graph(service, nodes, output)
    service.delete_graph(gid)
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(point=quad_point.tolist(), vector=quad_direction.tolist()))
    assert exc.value.category is ErrorCategory.NOT_FOUND
    with pytest.raises(ServiceError) as exc:
        service.get_graph("does-not-exist")
    assert exc.value.category is ErrorCategory.NOT_FOUND
