"""Unit tests: immutable FST structural validation."""

from __future__ import annotations

import pytest

from wfst_service.core.errors import TopologyError
from wfst_service.core.fst import Arc, Fst


@pytest.mark.unit
def test_start_state_out_of_range_is_topology_error() -> None:
    with pytest.raises(TopologyError):
        Fst.create("x", 1, 5, {0: 0.0}, ())


@pytest.mark.unit
def test_arc_references_unknown_state() -> None:
    with pytest.raises(TopologyError):
        Fst.create(
            "x", 2, 0, {1: 0.0},
            [Arc(0, 7, "a", "a", 0.0)],
        )


@pytest.mark.unit
def test_non_finite_cost_rejected() -> None:
    with pytest.raises(TopologyError):
        Fst.create(
            "x", 2, 0, {1: 0.0},
            [Arc(0, 1, "a", "a", float("inf"))],
        )
    with pytest.raises(TopologyError):
        Fst.create("x", 2, 0, {1: float("nan")}, ())


@pytest.mark.unit
def test_outgoing_is_immutable_tuple_and_frozen() -> None:
    fst = Fst.create(
        "x", 2, 0, {1: 0.0}, [Arc(0, 1, "a", "b", 0.2)]
    )
    assert isinstance(fst.outgoing(0), tuple)
    with pytest.raises(Exception):
        fst.name = "other"  # type: ignore[misc]


@pytest.mark.unit
def test_empty_language_internal_machine_is_allowed() -> None:
    # Composed machines may legitimately have no finals; explicit
    # user-facing specs still forbid that.
    fst = Fst.create("empty", 1, 0, {}, ())
    assert fst.is_final(0) is False
