"""Cycle analysis.

Declared support scope (also documented in README):

* **Pure epsilon cycles** -- a directed cycle in which *every* arc has
  ``ilabel == EPS`` and ``olabel == EPS`` -- are always rejected.  They
  consume and emit nothing, so they create infinitely many identical
  alignments and make shortest paths ill-defined.
* **Negative-cost cycles of any label pattern** are rejected via
  Bellman-Ford, because k-best costs would be unbounded below.
* Positive cycles that consume or emit real symbols are allowed
  (relations may be infinite; enumeration is bounded by ``k`` and the
  expansion budget).  Output-emitting input-epsilon cycles with zero
  total cost are out of the declared scope (assumed absent).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..corpus.symbols import EPS
from .fst import Fst


@dataclass(frozen=True, slots=True)
class CycleReport:
    has_epsilon_cycle: bool
    epsilon_witness: tuple[int, ...] = field(default_factory=tuple)
    has_negative_cycle: bool = False
    negative_witness: tuple[int, ...] = field(default_factory=tuple)
    negative_cycle_cost: float | None = None

    @property
    def ok(self) -> bool:
        return not self.has_epsilon_cycle and not self.has_negative_cycle


def find_epsilon_cycle(fst: Fst) -> tuple[int, ...]:
    """Return a witness state tuple for a pure (EPS,EPS) cycle, else ().

    Three-colour DFS over the sub-graph of silent arcs.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {s: WHITE for s in range(fst.num_states)}
    stack_trace: list[int] = []

    def dfs(u: int) -> tuple[int, ...]:
        colour[u] = GREY
        stack_trace.append(u)
        for arc in fst.outgoing(u):
            if arc.ilabel != EPS or arc.olabel != EPS:
                continue
            if colour[arc.dst] == GREY:
                idx = stack_trace.index(arc.dst)
                return tuple(stack_trace[idx:]) + (arc.dst,)
            if colour[arc.dst] == WHITE:
                found = dfs(arc.dst)
                if found:
                    return found
        stack_trace.pop()
        colour[u] = BLACK
        return ()

    for s in range(fst.num_states):
        if colour[s] == WHITE:
            witness = dfs(s)
            if witness:
                return witness
    return ()


def find_negative_cycle(fst: Fst) -> tuple[tuple[int, ...], float] | None:
    """Bellman-Ford with predecessor links (a virtual accepting node).

    Returns ``(witness_states, cycle_cost)`` where ``witness_states`` is a
    closed walk ``s0, s1, ..., s0`` along real arcs with strictly negative
    total cost, or ``None`` when no negative cycle is reachable from the
    start.

    Scope (deliberately conservative): every negative cycle *reachable
    from the start state* is rejected, including cycles that cannot
    themselves reach a final state.  Such a cycle cannot change a query
    answer, so rejecting it is safe (strictly stricter than necessary)
    and keeps the policy easy to state and audit.  Final weights are
    included in Bellman-Ford via edges into a virtual sink.

    Extraction follows the standard Bellman-Ford construction: after a
    full extra relaxation round, any still-relaxed node lies on (or can
    reach) a negative cycle; following predecessor links ``n+1`` times
    from such a node is guaranteed to land on a vertex of the cycle, and
    continuing around predecessors closes the witness.
    """
    n = fst.num_states
    virtual = n  # virtual accepting node (no outgoing edges)
    dist = [float("inf")] * (n + 1)
    pred: list[tuple[int, object] | None] = [None] * (n + 1)
    dist[fst.start] = 0.0

    edges: list[tuple[int, int, float, object]] = [
        (a.src, a.dst, a.cost, a) for a in fst.arcs
    ]
    for state, weight in fst.finals.items():
        edges.append((state, virtual, weight, None))

    last_relaxed = -1
    for _ in range(n + 1):
        last_relaxed = -1
        for u, v, weight, arc in edges:
            if dist[u] + weight < dist[v]:
                dist[v] = dist[u] + weight
                pred[v] = (u, arc)
                last_relaxed = v
    if last_relaxed == -1:
        return None

    # Walk predecessors n+1 times to reach a vertex guaranteed on cycle.
    x = last_relaxed
    for _ in range(n + 1):
        info = pred[x]
        if info is None:  # pragma: no cover - witness guarantees a pred
            return None
        x = info[0]

    # Walk predecessors back around the cycle, summing real arc weights.
    back = [x]
    total = 0.0
    current = x
    for _ in range(n + 2):
        info = pred[current]
        if info is None or info[1] is None:
            # A None arc denotes an edge into the virtual node, which has
            # no outgoing edges and cannot belong to a cycle; reaching it
            # here would contradict the guarantee on x.
            return None  # pragma: no cover
        previous, via_arc = info
        total += via_arc.cost
        back.append(previous)
        current = previous
        if previous == x:
            break
    else:  # pragma: no cover - failed to close within n+1 steps
        return None

    witness = tuple(reversed(back))
    return witness, total


def analyze_cycles(fst: Fst) -> CycleReport:
    eps_witness = find_epsilon_cycle(fst)
    neg = find_negative_cycle(fst)
    return CycleReport(
        has_epsilon_cycle=bool(eps_witness),
        epsilon_witness=eps_witness,
        has_negative_cycle=neg is not None,
        negative_witness=neg[0] if neg else (),
        negative_cycle_cost=neg[1] if neg else None,
    )
