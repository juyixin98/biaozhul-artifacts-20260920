"""Unit tests: cycle analysis (epsilon loops and negative-cost cycles)."""

from __future__ import annotations

import random

import pytest

from wfst_service.core.cycles import analyze_cycles, find_epsilon_cycle
from wfst_service.core.fst import Arc, Fst


def _fst(name: str, arcs: list[Arc], finals: dict[int, float],
         n: int = 4, start: int = 0) -> Fst:
    return Fst.create(name, n, start, finals, arcs)


@pytest.mark.unit
def test_pure_epsilon_self_loop_is_detected_with_witness() -> None:
    fst = _fst(
        "eps-self",
        [
            Arc(0, 0, "", "", -0.2),
            Arc(0, 1, "a", "a", 0.0),
        ],
        {1: 0.0},
    )
    report = analyze_cycles(fst)
    assert report.has_epsilon_cycle is True
    assert report.epsilon_witness[0] == report.epsilon_witness[-1] == 0
    assert report.ok is False


@pytest.mark.unit
def test_multi_state_silent_cycle_is_detected() -> None:
    fst = _fst(
        "eps-chain",
        [
            Arc(0, 1, "", "", 0.0),
            Arc(1, 2, "", "", 0.0),
            Arc(2, 1, "", "", 0.0),
            Arc(0, 3, "a", "a", 0.0),
        ],
        {3: 0.0},
    )
    witness = find_epsilon_cycle(fst)
    assert set(witness) >= {1, 2}
    assert witness[0] == witness[-1]


@pytest.mark.unit
def test_negative_cost_real_label_cycle_is_detected() -> None:
    fst = _fst(
        "neg",
        [
            Arc(0, 1, "a", "a", 0.0),
            Arc(1, 1, "x", "x", -1.0),
            Arc(1, 2, "b", "b", 0.0),
        ],
        {2: 0.0},
        n=3,
    )
    report = analyze_cycles(fst)
    assert report.has_negative_cycle is True
    assert report.negative_witness
    assert report.negative_cycle_cost is not None
    assert report.negative_cycle_cost <= -0.999


@pytest.mark.unit
def test_positive_cycle_is_allowed() -> None:
    fst = _fst(
        "pos-loop",
        [
            Arc(0, 0, "x", "x", 1.0),
            Arc(0, 1, "a", "a", 0.0),
        ],
        {1: 0.0},
    )
    report = analyze_cycles(fst)
    assert report.ok is True


@pytest.mark.unit
def test_input_epsilon_output_label_cycle_is_not_pure_epsilon() -> None:
    # (eps, "e") insertion loop: emits a real symbol, so it is NOT a pure
    # epsilon cycle even though the input tape is epsilon.
    fst = _fst(
        "insert-loop",
        [
            Arc(0, 0, "", "e", 1.5),
            Arc(0, 1, "a", "a", 0.0),
        ],
        {1: 0.0},
    )
    report = analyze_cycles(fst)
    assert report.has_epsilon_cycle is False
    assert report.ok is True


@pytest.mark.unit
def test_acyclic_negative_weights_are_allowed() -> None:
    # Negative arcs without a reachable negative cycle are legitimate
    # (Bellman-Ford potentials must stay finite).
    fst = _fst(
        "discount",
        [
            Arc(0, 1, "a", "a", -2.0),
            Arc(1, 2, "b", "b", 1.0),
        ],
        {2: 0.0},
        n=3,
    )
    report = analyze_cycles(fst)
    assert report.ok is True


@pytest.mark.unit
def test_negative_cycle_witness_fuzz_is_always_well_formed() -> None:
    """Regression test for predecessor-chain witness reconstruction.

    An earlier implementation returned malformed witnesses (e.g.
    ``(2, 0, 0)``) for a large fraction of cyclic random graphs.  Every
    reported witness must now be a closed walk along real arcs whose
    summed weight is negative.
    """
    rng = random.Random(20260928)
    found = 0
    for _ in range(2500):
        n = rng.randint(2, 6)
        finals = {rng.randrange(n): 0.0}
        edges: dict[tuple[int, int], float] = {}
        for _ in range(rng.randint(2, 11)):
            src, dst = rng.randrange(n), rng.randrange(n)
            weight = round(
                rng.choice([0.0, 1.0, -1.0, -0.5, 2.0, -2.0]), 2
            )
            edges[(src, dst)] = min(edges.get((src, dst), 1e18), weight)
        arcs = [Arc(s, d, "a", "a", w) for (s, d), w in edges.items()]
        try:
            fst = Fst.create("fuzz", n, 0, finals, arcs)
        except Exception:  # pragma: no cover - generator stays valid
            continue
        result = analyze_cycles(fst)
        if not result.has_negative_cycle:
            continue
        found += 1
        witness = result.negative_witness
        assert len(witness) >= 2
        assert witness[0] == witness[-1]
        by_edge = {(a.src, a.dst): a.cost for a in fst.arcs}
        total = 0.0
        for src, dst in zip(witness, witness[1:]):
            assert (src, dst) in by_edge, (
                f"witness jumps non-adjacent states {src}->{dst}: {witness}"
            )
            total += by_edge[(src, dst)]
        assert total < -1e-9
        assert result.negative_cycle_cost == pytest.approx(total, abs=1e-9)

    # The fixed seed must actually exercise negative cycles.
    assert found >= 500
