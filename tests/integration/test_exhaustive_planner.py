"""Phase-3 case B: exhaustive checkpoint enumeration cross-checks.

Every checkpoint subset of the small chain graph is enumerated (2**k).  For
each subset we assert *concrete* numbers from the core simulator against the
*independently implemented* reference liveness simulator: peak memory,
forward/recompute/backward costs and the exact recomputed node set.
"""

from __future__ import annotations

from itertools import combinations

import pytest

from app.core import ops as ops_mod
from app.core.memory import simulate
from app.reference.liveness import LivenessSim


def _internal_ids(fx) -> list[str]:
    g = fx.graph()
    return [
        nid for nid in g.order
        if g.nodes[nid].op not in ops_mod.ROOT_OPS and nid != g.target
    ]


def _raw_spec(fx) -> dict:
    return {
        "nodes": [
            {"id": n.id, "op": n.op, "inputs": list(n.inputs),
             "params": dict(n.params)}
            for n in fx.nodes
        ],
        "target": fx.target,
    }


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["counter", "snapshot"])
def test_all_subsets_match_independent_liveness_simulator(
    linear_chain, strategy
) -> None:
    fx = linear_chain
    g = fx.graph()
    internal = _internal_ids(fx)
    ref = LivenessSim(_raw_spec(fx))
    assert len(internal) == 4  # 2**4 == 16 subsets

    checked = 0
    for r in range(len(internal) + 1):
        for subset in combinations(internal, r):
            core = simulate(g, list(subset), rng_strategy=strategy)
            indep = ref.simulate(list(subset), rng_strategy=strategy)
            assert core.peak_memory == indep["peak_memory"], subset
            assert core.forward_flops == indep["forward_flops"], subset
            assert core.recompute_flops == indep["recompute_flops"], subset
            assert core.backward_flops == indep["backward_flops"], subset
            core_replay = tuple(sorted(
                list(core.recomputed_nodes) + list(core.mask_only_nodes)
            ))
            assert core_replay == indep["recomputed_nodes"], subset
            checked += 1
    assert checked == 16


@pytest.mark.integration
def test_retaining_everything_has_zero_recompute_and_largest_peak(
    linear_chain,
) -> None:
    fx = linear_chain
    g = fx.graph()
    internal = _internal_ids(fx)
    full = simulate(g, internal, rng_strategy="counter")
    none = simulate(g, [], rng_strategy="counter")
    assert full.recompute_flops == 0
    assert none.recompute_flops > 0
    assert full.peak_memory > none.peak_memory
    # The full-recompute plan replays exactly the internal nodes some VJP
    # reads. `a2` (the relu feeding reduce_sum) is NOT among them: the
    # reduce_sum VJP only needs a shape, never its activation.
    assert set(none.recomputed_nodes) == {"h1", "a1", "h2"}


@pytest.mark.integration
def test_pareto_frontier_trades_memory_for_compute(linear_chain) -> None:
    # Enumerate (peak, recompute) points and take the nondominated frontier.
    fx = linear_chain
    g = fx.graph()
    internal = _internal_ids(fx)
    points: set[tuple[int, int]] = set()
    for r in range(len(internal) + 1):
        for subset in combinations(internal, r):
            sim = simulate(g, list(subset))
            points.add((sim.peak_memory, sim.recompute_flops))
    frontier = sorted(
        (p for p in points
         if not any(q != p and q[0] <= p[0] and q[1] <= p[1] for q in points)),
        key=lambda p: p[0],
    )
    # Along the frontier, raising the peak must strictly cut recompute:
    # the classic memory <-> extra-compute trade-off.
    for (p0, c0), (p1, c1) in zip(frontier, frontier[1:]):
        assert p0 < p1 and c0 > c1
    # Concrete anchors for this fixture: cheapest peak costs recompute,
    # and the zero-recompute point needs more memory.
    assert frontier[0] == (84, 48)
    assert (90, 0) in frontier


@pytest.mark.integration
def test_shared_subgraph_is_recomputed_at_most_once(branching) -> None:
    fx = branching
    g = fx.graph()
    # Drop every checkpoint: `shared` and W3 are each consumed twice.
    sim = simulate(g, [], rng_strategy="counter")
    # The shared relu must appear exactly once in the recomputed list even
    # though it serves two branches during the backward pass.
    assert sim.recomputed_nodes.count("shared") == 1
    # Memoisation verified structurally: recomputed list has unique nodes.
    assert len(sim.recomputed_nodes) == len(set(sim.recomputed_nodes))
