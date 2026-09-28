"""Tests for constraint validation: conflict classes are distinguished."""

from __future__ import annotations

import pytest

from entity_resolution.clustering import canon_pair, validate_constraints
from entity_resolution.errors import ConstraintConflictError, InvalidRequestError


def test_self_link_is_input_error_not_state_conflict() -> None:
    with pytest.raises(InvalidRequestError) as exc:
        canon_pair("a", "a")
    assert exc.value.details["reason"] == "self_link"


def test_unknown_record_is_input_error() -> None:
    with pytest.raises(InvalidRequestError) as exc:
        validate_constraints(["a"], must=[("a", "ghost")])
    assert exc.value.details["reason"] == "unknown_record"


def test_direct_contradiction_detected() -> None:
    with pytest.raises(ConstraintConflictError) as exc:
        validate_constraints(["a", "b"], must=[("a", "b")], cannot=[("b", "a")])
    assert exc.value.details["reason"] == "direct_contradiction"
    assert exc.value.category == "STATE_CONFLICT"


def test_transitive_must_closure_contradicts_cannot() -> None:
    # a~b and b~c force {a,b,c}; a cannot c must therefore be rejected even
    # though no direct a-c must-link exists.
    with pytest.raises(ConstraintConflictError) as exc:
        validate_constraints(
            ["a", "b", "c"], must=[("a", "b"), ("b", "c")], cannot=[("a", "c")]
        )
    assert exc.value.details["reason"] == "must_link_closure"
    assert set(exc.value.details["must_component"]) == {"a", "b", "c"}


def test_cannot_inside_locked_block_rejected() -> None:
    with pytest.raises(ConstraintConflictError) as exc:
        validate_constraints(
            ["a", "b"],
            cannot=[("a", "b")],
            locked_blocks=[frozenset({"a", "b"})],
        )
    assert exc.value.details["reason"] == "inside_locked_cluster"


def test_must_across_two_locks_rejected() -> None:
    with pytest.raises(ConstraintConflictError) as exc:
        validate_constraints(
            ["a", "b"],
            must=[("a", "b")],
            locked_blocks=[frozenset({"a"}), frozenset({"b"})],
        )
    assert exc.value.details["reason"] == "cross_locked_must"


def test_consistent_constraints_pass() -> None:
    cs = validate_constraints(
        ["a", "b", "c"],
        must=[("a", "b")],
        cannot=[("a", "c"), ("b", "c")],
    )
    assert ("a", "b") in cs.must
    assert ("a", "c") in cs.cannot
