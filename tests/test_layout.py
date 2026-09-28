"""Layout binding: flat vectors must match the declared input layout."""

from __future__ import annotations

import numpy as np
import pytest

from hvp_service.errors import ErrorCategory, ServiceError
from hvp_service.service import HvpRequestData
from hvp_service.tensor import Layout

from .conftest import make_graph, quadratic_spec, two_slot_spec


def test_layout_flatten_unflatten_roundtrip() -> None:
    layout = Layout([("w", (2, 2)), ("b", ()), ("v", (3,))])
    values = {
        "w": np.array([[1.0, 2.0], [3.0, 4.0]]),
        "b": np.asarray(5.0),
        "v": np.array([6.0, 7.0, 8.0]),
    }
    flat = layout.flatten(values)
    assert flat.shape == (8,)
    assert flat.tolist() == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    back = layout.unflatten(flat)
    for name in values:
        np.testing.assert_array_equal(back[name], values[name])


def test_layout_rejects_duplicate_names() -> None:
    with pytest.raises(ServiceError) as exc:
        Layout([("x", (2,)), ("x", (1,))])
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION


def test_layout_rejects_wrong_sized_vector() -> None:
    layout = Layout([("x", (3,))])
    with pytest.raises(ServiceError) as exc:
        layout.unflatten(np.zeros(2))
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION
    assert exc.value.details["expected_size"] == 3
    assert exc.value.details["actual_size"] == 2


def test_two_slot_gradient_follows_layout_order(service) -> None:
    """f(w, b) = sum(w^2) + b^2 -> grad = [2 w0, 2 w1, 2 b], hvp = 2 v."""
    nodes, output = two_slot_spec()
    gid = make_graph(service, nodes, output)
    result = service.hvp(
        gid,
        HvpRequestData(point=[3.0, -1.0, 0.5], vector=[1.0, 2.0, -2.0]),
    )
    np.testing.assert_allclose(result.gradient, [6.0, -2.0, 1.0], atol=1e-12)
    np.testing.assert_allclose(result.hvp, [2.0, 4.0, -4.0], atol=1e-12)
    assert result.value == pytest.approx(9.0 + 1.0 + 0.25, abs=1e-12)


def test_hvp_rejects_mismatched_point_and_vector(service) -> None:
    nodes, output = quadratic_spec()
    gid = make_graph(service, nodes, output)
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(point=[1.0, 2.0], vector=[1.0, 1.0, 1.0]))
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(point=[1.0, 2.0, 3.0], vector=[1.0]))
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION


def test_hvp_rejects_non_finite_inputs(service) -> None:
    nodes, output = quadratic_spec()
    gid = make_graph(service, nodes, output)
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(point=[1.0, float("nan"), 3.0], vector=[1.0, 0.0, 0.0]))
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(point=[1.0, 2.0, 3.0], vector=[float("inf"), 0.0, 0.0]))
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION
