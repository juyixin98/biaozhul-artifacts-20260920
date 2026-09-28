"""Unit tests: silent-arc removal and epsilon-filtered composition."""

from __future__ import annotations

import pytest

from wfst_service.core.compose import compose
from wfst_service.core.eps_removal import remove_silent_epsilon
from wfst_service.core.fst import Arc, Fst


@pytest.mark.unit
def test_silent_arc_shortcut_preserves_path_weight() -> None:
    # 0 --(a,a,0)--> 1 ==(eps,eps,0.7)==> 2 --(b,b,0)--> 3(final)
    fst = Fst.create(
        "silent",
        4,
        0,
        {3: 0.0},
        [
            Arc(0, 1, "a", "a", 0.0),
            Arc(1, 2, "", "", 0.7),
            Arc(2, 3, "b", "b", 0.0),
        ],
    )
    reduced = remove_silent_epsilon(fst)
    assert all(not (a.ilabel == "" and a.olabel == "") for a in reduced.arcs)
    keyed = {(a.src, a.dst, a.ilabel, a.olabel): a.cost for a in reduced.arcs}
    # The reading a,b must survive with the silent hop folded in: arc
    # 1 -> 3 on 'b' carries the 0.7 silent cost; a direct 0 -> 3 shortcut
    # must NOT appear because reaching state 1 requires consuming 'a'.
    assert keyed[(1, 3, "b", "b")] == pytest.approx(0.7)
    assert keyed[(0, 1, "a", "a")] == pytest.approx(0.0)
    assert (0, 3, "b", "b") not in keyed
    assert reduced.is_final(3)
    assert not reduced.is_final(1)


@pytest.mark.unit
def test_composition_identity_left_unchanged_relation() -> None:
    a = Fst.create(
        "a", 2, 0, {1: 0.0},
        [Arc(0, 1, "x", "y", 0.5)],
    )
    b = Fst.create(
        "b", 2, 0, {1: 0.0},
        [Arc(0, 1, "y", "z", 0.25)],
    )
    composed, trace = compose(a, b, "ab")
    assert composed.num_states == 2
    arc = composed.arcs[0]
    assert (arc.ilabel, arc.olabel) == ("x", "z")
    assert arc.cost == pytest.approx(0.75)
    assert trace.synchronised_moves == 1
    assert trace.filter_blocked == 0


@pytest.mark.unit
def test_epsilon_insert_delete_does_not_duplicate_paths() -> None:
    # Bounded, acyclic version: left deletes x with an A-alone arc
    # (x, eps); right inserts z with a B-alone arc (eps, z).  The input
    # "xa" has exactly one accepting derivation -- schedule B* then A* --
    # and output "za" at cost 2.0.  A naive interleaving would count the
    # same pair of epsilon moves twice.
    left = Fst.create(
        "del", 3, 0, {2: 0.0},
        [
            Arc(0, 1, "x", "", 1.0),
            Arc(1, 2, "a", "a", 0.0),
        ],
    )
    right = Fst.create(
        "ins", 3, 0, {2: 0.0},
        [
            Arc(0, 1, "", "z", 1.0),
            Arc(1, 2, "a", "a", 0.0),
        ],
    )
    composed, trace = compose(left, right, "del_ins")
    assert trace.filter_blocked >= 1

    from wfst_service.core.search import kbest

    result, search_trace = kbest(composed, "xa", 5, run_id="unit-delins")
    assert [(o.output, o.cost) for o in result.outputs] == [("za", 2.0)]
    assert result.complete is True
    assert search_trace.verdict == "complete:language_exhausted"


@pytest.mark.unit
def test_input_and_output_epsilon_never_synchronise() -> None:
    # Left consumes 'a' silently: (a, eps).  Right inserts 'b' without
    # consuming: (eps, b).  These are one-sided epsilon moves; they must
    # be SCHEDULED (B* A*), never fused into a single (eps, eps) paired
    # transition.  Query "a" must produce exactly one output "b" via one
    # derivation, not an empty output or two duplicate derivations.
    left = Fst.create(
        "l", 2, 0, {1: 0.0},
        [Arc(0, 1, "a", "", 0.0)],
    )
    right = Fst.create(
        "r", 2, 0, {1: 0.0},
        [Arc(0, 1, "", "b", 0.0)],
    )
    composed, _trace = compose(left, right, "sides")

    # Independent structural check: no arc may carry (eps, eps), since
    # neither operand had one and a one-sided epsilon pair must not fuse.
    assert all(not (a.ilabel == "" and a.olabel == "") for a in composed.arcs)

    def count_full_paths(fst: Fst) -> int:
        count = 0

        def dfs(s: int, seen_edges: frozenset) -> None:
            nonlocal count
            if fst.is_final(s):
                count += 1
            for arc in fst.outgoing(s):
                edge = (s, arc.dst, arc.ilabel, arc.olabel)
                if edge in seen_edges:
                    continue
                dfs(arc.dst, seen_edges | {edge})

        dfs(fst.start, frozenset())
        return count

    assert count_full_paths(composed) == 1

    from wfst_service.core.search import kbest

    result, _ = kbest(composed, "a", 3, run_id="unit-sides")
    assert [(o.output, o.cost) for o in result.outputs] == [("b", 0.0)]


@pytest.mark.unit
def test_disjoint_middle_alphabet_yields_empty_language_machine() -> None:
    left = Fst.create(
        "l", 2, 0, {1: 0.0}, [Arc(0, 1, "a", "x", 0.0)]
    )
    right = Fst.create(
        "r", 2, 0, {1: 0.0}, [Arc(0, 1, "q", "b", 0.0)]
    )
    composed, trace = compose(left, right, "disjoint")
    assert len(composed.finals) == 0
    assert trace.dangling_middle_labels == ("x",)
