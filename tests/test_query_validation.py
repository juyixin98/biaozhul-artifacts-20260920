"""Query-constraint validation edge branches and failure categories."""
from __future__ import annotations

import math

import pytest

from app.errors import InvalidConstraintError
from app.miner.constraints import validate_query
from app.models import MineRequest
from tests.conftest import build_corpus


def _validate(sequences, **params):
    corpus = build_corpus(sequences, corpus_id="c")
    params.setdefault("corpus_id", "c")
    params.setdefault("min_support", 1)
    return validate_query(corpus, MineRequest(**params))


TS_CORPUS = {"s1": [("A", 0.0), ("B", 5.0)], "s2": [("A", 0.0), ("B", 5.0)]}
NO_TS_CORPUS = {"s1": [("A", None), ("B", None)]}


def test_boolean_min_support_is_rejected_at_model_layer():
    # bool must not sneak through as 0/1; StrictNumber rejects it when the
    # request model is built (surfaced as invalid_request_body over HTTP).
    corpus = build_corpus(TS_CORPUS, corpus_id="c")
    with pytest.raises(Exception):
        MineRequest(corpus_id="c", min_support=True)
    with pytest.raises(Exception):
        MineRequest(corpus_id="c", min_support=False)


def test_non_numeric_min_support_type_is_rejected():
    corpus = build_corpus(TS_CORPUS, corpus_id="c")
    with pytest.raises(Exception):
        # pydantic StrictNumber rejects the type at model construction.
        MineRequest(corpus_id="c", min_support=[1])


@pytest.mark.parametrize("bad_float", [float("nan"), float("inf"), -float("inf"), 0.0, 1.5])
def test_fractional_support_rejects_non_finite_and_out_of_range(bad_float):
    with pytest.raises(InvalidConstraintError):
        _validate(TS_CORPUS, min_support=bad_float)


def test_fractional_support_ceil_math():
    constraints = _validate(TS_CORPUS, min_support=0.6)  # ceil(0.6*2)=2
    assert constraints.min_support == 2
    constraints = _validate(TS_CORPUS, min_support=0.01)  # ceil -> 1
    assert constraints.min_support == 1


def test_max_pattern_length_below_one_rejected():
    with pytest.raises(InvalidConstraintError):
        _validate(TS_CORPUS, max_pattern_length=0)


def test_max_pattern_length_is_capped_by_settings():
    constraints = _validate(TS_CORPUS, max_pattern_length=10**9)
    from app.config import settings
    assert constraints.max_pattern_length == settings.max_pattern_length


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), -0.1, -1.0])
def test_bad_time_gap_values_rejected(bad_time):
    with pytest.raises(InvalidConstraintError):
        _validate(TS_CORPUS, max_gap_time=bad_time)


def test_time_gap_without_timestamps_rejected():
    with pytest.raises(InvalidConstraintError) as exc:
        _validate(NO_TS_CORPUS, max_gap_time=1)
    assert exc.value.details["constraint"] == "max_gap_time"


def test_pair_ok_time_gap_exactly_at_boundary():
    c = _validate(TS_CORPUS, max_gap_time=5.0, max_gap_position=2)
    assert c.pair_ok(0, 1, 0.0, 5.0) is True   # exact boundary inclusive
    assert c.pair_ok(0, 2, 0.0, 6.0) is False  # over time
    assert c.pair_ok(0, 3, 0.0, 5.0) is False  # over position
    # Missing timestamps fail a declared time gap.
    assert c.pair_ok(0, 1, None, None) is False


def test_pair_ok_without_constraints_always_accepts_forward_pairs():
    c = _validate(TS_CORPUS)
    assert c.pair_ok(0, 100, None, None) is True


def test_zero_time_gap_accepts_equal_timestamps():
    c = _validate(TS_CORPUS, max_gap_time=0.0)
    assert c.pair_ok(0, 1, 5.0, 5.0) is True
    assert c.pair_ok(0, 1, 5.0, 5.1) is False
