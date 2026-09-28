"""Unit tests: k-best search ordering, dedup, budget, no-path."""

from __future__ import annotations

import pytest

from wfst_service.corpus.errors import BudgetExhausted
from wfst_service.core.fst import Arc, Fst
from wfst_service.core.search import kbest


def _ambiguous_fst() -> Fst:
    # Two distinct derivations of the SAME output "ab" (costs 1.0 and
    # 0.5), built only from single-character arcs (one symbol per arc,
    # epsilon allowed), to test dedup at minimum alignment cost.
    return Fst.create(
        "amb",
        6,
        0,
        {2: 0.0, 5: 0.0},
        [
            # Cheap alignment: a -> a, b -> b, cost 0.5.
            Arc(0, 1, "a", "a", 0.0),
            Arc(1, 2, "b", "b", 0.5),
            # Dear alignment: a -> eps, b -> a, eps -> b, cost 1.0.
            Arc(0, 3, "a", "", 0.0),
            Arc(3, 4, "b", "a", 0.5),
            Arc(4, 5, "", "b", 0.5),
        ],
    )


@pytest.mark.unit
def test_duplicate_alignments_collapse_to_minimum_cost() -> None:
    fst = _ambiguous_fst()
    result, trace = kbest(fst, "ab", 5, run_id="unit-dedup")
    pairs = [(o.output, round(o.cost, 3)) for o in result.outputs]
    # "ab" appears once, at the cheaper alignment cost 0.5.
    assert pairs == [("ab", 0.5)]
    assert result.complete is True
    assert "dedup" in "\n".join(trace.as_lines())


@pytest.mark.unit
def test_ordering_is_cost_then_lexicographic() -> None:
    # Equal-cost outputs "b" and "a": lexicographic must win regardless of
    # discovery order.
    fst = Fst.create(
        "tie", 3, 0, {1: 0.0, 2: 0.0},
        [
            Arc(0, 1, "x", "b", 0.0),
            Arc(0, 2, "x", "a", 0.0),
        ],
    )
    result, _ = kbest(fst, "x", 2, run_id="unit-tie")
    assert [o.output for o in result.outputs] == ["a", "b"]
    assert all(o.cost == 0.0 for o in result.outputs)


@pytest.mark.unit
def test_k_truncation_respects_equal_cost_tie_class() -> None:
    # k=1 with three zero-cost outputs: still only one returned and it is
    # the lexicographically smallest, with complete=True.
    fst = Fst.create(
        "tri", 4, 0, {1: 0.0, 2: 0.0, 3: 0.0},
        [
            Arc(0, 1, "x", "c", 0.0),
            Arc(0, 2, "x", "a", 0.0),
            Arc(0, 3, "x", "b", 0.0),
        ],
    )
    result, _ = kbest(fst, "x", 1, run_id="unit-k1")
    assert [o.output for o in result.outputs] == ["a"]
    assert result.complete is True


@pytest.mark.unit
def test_no_path_is_complete_and_empty() -> None:
    fst = Fst.create(
        "np", 2, 0, {1: 0.0}, [Arc(0, 1, "a", "a", 0.0)]
    )
    result, trace = kbest(fst, "zzz", 3, run_id="unit-nopath")
    assert result.outputs == ()
    assert result.accepted is False
    assert result.complete is True
    assert trace.verdict == "rejected:no_path"


@pytest.mark.unit
def test_budget_exhaustion_is_raised_as_incomplete() -> None:
    # Positive-cost epsilon insertion self loop emits arbitrary "e"s:
    # the language is infinite, so k=100 cannot complete; a small budget
    # must fail loudly with budget_exhausted (never a truncated success).
    fst = Fst.create(
        "inf", 2, 0, {0: 0.0, 1: 0.0},
        [
            Arc(0, 0, "", "e", 1.5),
            Arc(0, 1, "a", "a", 0.0),
        ],
    )
    with pytest.raises(BudgetExhausted) as excinfo:
        kbest(fst, "a", 100, run_id="unit-budget", budget=20)
    assert excinfo.value.code == "budget_exhausted"
    assert excinfo.value.expansions > 0
    assert excinfo.value.budget == 20
    assert "incomplete" in str(excinfo.value)


@pytest.mark.unit
def test_final_weight_participates_in_ordering() -> None:
    # Same output path shape, different terminal weights.
    fst = Fst.create(
        "fw", 3, 0, {1: 2.0, 2: 0.0},
        [
            Arc(0, 1, "x", "y", 0.0),
            Arc(0, 2, "x", "z", 1.5)],
    )
    # "y": 2.0 total; "z": 1.5 total -> z first despite cheaper arc on y.
    result, _ = kbest(fst, "x", 2, run_id="unit-finalw")
    assert [(o.output, o.cost) for o in result.outputs] == [
        ("z", 1.5),
        ("y", 2.0),
    ]


@pytest.mark.unit
def test_negative_acyclic_weights_shortest_path() -> None:
    # A negative discount arc must be chosen by shortest cost.
    fst = Fst.create(
        "disc", 4, 0, {2: 0.0, 3: 0.0},
        [
            Arc(0, 1, "a", "a", -1.0),
            Arc(1, 2, "b", "b", 0.0),
            Arc(0, 3, "a", "q", 5.0),
        ],
    )
    result, _ = kbest(fst, "ab", 3, run_id="unit-neg")
    assert result.outputs[0].output == "ab"
    assert result.outputs[0].cost == -1.0
