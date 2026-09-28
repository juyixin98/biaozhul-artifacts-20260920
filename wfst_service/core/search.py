"""Bounded shortest-output enumeration.

Given a transducer relation and a fixed input string, enumerate the
*distinct* output strings ordered by (total cost, lexicographic output).

Algorithm:

1. Compose ``acceptor(input) o fst`` so every start-to-final path
   consumes exactly the requested input; the resulting machine has no
   pure ``(EPS, EPS)`` arcs by construction (see
   :mod:`wfst_service.core.compose`).
2. Compute a consistent heuristic ``h[state]`` = minimum remaining cost
   from the state to acceptance (arc costs plus the final weight) with
   Bellman-Ford on the reversed graph.
3. Extend the graph with one virtual goal node ``GOAL`` and edges
   ``final state -> GOAL`` weighted by the state's final weight.  With
   ``h[GOAL] = 0`` this is an admissible, consistent A* heuristic, so
   goal configurations pop in non-decreasing true total cost.
4. Best-first search over configurations ``(state, output)``.  Equivalent
   alignments of one output string converge at the single config
   ``(GOAL, output)`` and collapse to their minimum cost.

Tie fairness: heap order at equal priority is discovery order, so once
``k`` distinct outputs exist, every further config with priority no
greater than the k-th cost is drained before ranking, letting the
lexicographic tie-break choose fairly.

A hard expansion budget bounds work against positive-cost
symbol-emitting epsilon loops (relations may be infinite): exhaustion
raises :class:`BudgetExhausted` -- the answer is reported as
**incomplete**, never as a successful truncation.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from ..corpus.errors import BudgetExhausted
from ..corpus.symbols import EPS
from .compose import compose
from .errors import TopologyError
from .fst import Fst

_INF = float("inf")
_FLOAT_EPS = 1e-12
_GOAL = -1  # virtual node id in configuration space only


@dataclass(frozen=True, slots=True)
class Output:
    output: str
    cost: float
    order_key: tuple


@dataclass(frozen=True, slots=True)
class KBestResult:
    input: str
    fst: str
    outputs: tuple[Output, ...]
    complete: bool
    expansions: int
    settled_configurations: int
    budget: int

    @property
    def accepted(self) -> bool:
        return bool(self.outputs)


@dataclass(frozen=True, slots=True)
class SearchTrace:
    run_id: str
    input: str
    fst: str
    k: int
    budget: int
    steps: tuple[str, ...] = field(default_factory=tuple)
    expansions: int = 0
    settled: int = 0
    verdict: str = ""

    def as_lines(self) -> list[str]:
        lines = [
            f"search run={self.run_id} fst={self.fst!r} "
            f"input={self.input!r} k={self.k} budget={self.budget}",
            f"verdict: {self.verdict}",
            f"expansions={self.expansions} settled={self.settled}",
        ]
        return lines + [f"  {s}" for s in self.steps]


def backward_potentials(fst: Fst) -> list[float]:
    """Bellman-Ford on the reversed graph (virtual goal is the source).

    ``h[s]`` is the cheapest cost of reaching acceptance from ``s``,
    final weights included.  Raises :class:`TopologyError` on a reachable
    negative cycle.
    """
    n = fst.num_states
    h = [_INF] * n
    for s, w in fst.finals.items():
        h[s] = w

    rev: dict[int, list[tuple[int, float]]] = {s: [] for s in range(n)}
    for arc in fst.arcs:
        rev[arc.dst].append((arc.src, arc.cost))

    for _ in range(n):
        changed = False
        for v in range(n):
            hv = h[v]
            if hv == _INF:
                continue
            for u, w in rev[v]:
                cand = w + hv
                if cand < h[u] - _FLOAT_EPS:
                    h[u] = cand
                    changed = True
        if not changed:
            return h
    raise TopologyError(
        f"fst {fst.name!r}: negative-cost cycle reachable on the way to a "
        "final state (shortest output is unbounded below)"
    )


def kbest(
    fst: Fst,
    text: str,
    k: int,
    *,
    run_id: str,
    budget: int = 100_000,
    trace: bool = True,
) -> tuple[KBestResult, SearchTrace]:
    """Return up to ``k`` distinct lowest-cost outputs for ``text``.

    Ordering: total cost ascending, then output lexicographically.
    Raises :class:`BudgetExhausted` when the expansion budget is spent
    before the answer (including an equal-cost tie set) is proven
    complete.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    if budget <= 0:
        raise ValueError("budget must be positive")

    steps: list[str] = []
    acceptor = Fst.acceptor(text, name=f"<acceptor:{text!r}>")
    query_fst, comp_trace = compose(acceptor, fst, name=f"query({fst.name})")
    if trace:
        steps.extend(comp_trace.as_lines())
        steps.append(
            f"query machine: states={query_fst.num_states} "
            f"arcs={len(query_fst.arcs)} finals={len(query_fst.finals)}"
        )

    h = backward_potentials(query_fst)
    if h[query_fst.start] == _INF:
        result = KBestResult(
            input=text, fst=fst.name, outputs=(), complete=True,
            expansions=0, settled_configurations=0, budget=budget,
        )
        search_trace = SearchTrace(
            run_id=run_id, input=text, fst=fst.name, k=k, budget=budget,
            steps=tuple(steps + ["start cannot reach a final -> empty language"]),
            expansions=0, settled=0, verdict="rejected:no_path",
        )
        return result, search_trace

    if trace:
        steps.append(
            "backward potentials (min remaining cost to virtual goal): "
            + ", ".join(
                f"{s}:{h[s]:g}"
                for s in range(query_fst.num_states)
                if h[s] != _INF
            )
        )

    # Heap entries: (priority f=g+h, tie, g, state, output).
    counter = 0
    heap: list[tuple[float, int, float, int, str]] = [
        (h[query_fst.start], counter, 0.0, query_fst.start, "")
    ]
    # Minimum g at which a configuration has already been settled.
    settled: dict[tuple[int, str], float] = {}
    found: dict[str, float] = {}
    expansions = 0

    def fail_budget(spent: int) -> BudgetExhausted:
        return BudgetExhausted(
            f"search budget exhausted after {spent} expansions while looking "
            f"for {k} outputs (found {len(found)} distinct); result is "
            "incomplete",
            expansions=spent, budget=budget,
        )

    def expand(g: float, state: int, output: str) -> None:
        """Push successors of one settled configuration."""
        nonlocal counter
        if state == _GOAL:
            return
        # Virtual goal edge from final states.
        if state in query_fst.finals:
            total = g + query_fst.final_cost(state)
            counter += 1
            heapq.heappush(heap, (total, counter, total, _GOAL, output))
        for arc in query_fst.outgoing(state):
            ng = g + arc.cost
            if h[arc.dst] == _INF:
                continue
            nout = output if arc.olabel == EPS else output + arc.olabel
            nconfig = (arc.dst, nout)
            if settled.get(nconfig, _INF) <= ng + _FLOAT_EPS:
                continue
            counter += 1
            heapq.heappush(heap, (ng + h[arc.dst], counter, ng, arc.dst, nout))

    while heap:
        f_prio, _tie, g, state, output = heapq.heappop(heap)
        config = (state, output)
        if settled.get(config, _INF) <= g + _FLOAT_EPS:
            continue
        settled[config] = g
        expansions += 1
        if expansions > budget:
            raise fail_budget(expansions)

        if state == _GOAL:
            # Standard A* guarantee: popped goal costs are non-decreasing
            # and optimal for the configuration.
            if output not in found:
                found[output] = g
                if trace:
                    steps.append(
                        f"settle goal output={output!r} total_cost={g:g} "
                        f"[#{len(found)} distinct]"
                    )
            if len(found) >= k:
                threshold = g
                break

        expand(g, state, output)
    else:
        threshold = _INF  # heap exhausted

    # Equal-cost tie drain: pop everything with priority <= threshold so
    # lexicographic ordering at the k-th cost is decided on the full tie
    # class rather than discovery order.
    if len(found) >= k:
        while heap and heap[0][0] <= threshold + _FLOAT_EPS:
            f_prio, _tie, g, state, output = heapq.heappop(heap)
            config = (state, output)
            if settled.get(config, _INF) <= g + _FLOAT_EPS:
                continue
            settled[config] = g
            expansions += 1
            if expansions > budget:
                raise fail_budget(expansions)
            if state == _GOAL and g <= threshold + _FLOAT_EPS:
                if output not in found:
                    found[output] = g
                    if trace:
                        steps.append(
                            f"tie-drain goal output={output!r} "
                            f"total_cost={g:g}"
                        )
            expand(g, state, output)

    if not heap and len(found) < k:
        complete = True
        verdict = "complete:language_exhausted"
    elif len(found) >= k:
        complete = True
        verdict = f"complete:k_reached({min(k, len(found))})"
    else:  # pragma: no cover - every other path raises above
        raise fail_budget(expansions)

    ranked = sorted(found.items(), key=lambda kv: (kv[1], kv[0]))[:k]
    outputs = tuple(
        Output(output=o, cost=c, order_key=(c, o)) for o, c in ranked
    )
    if trace:
        steps.append(
            "ranked outputs: "
            + (", ".join(f"{o.output!r}@{o.cost:g}" for o in outputs) or "-")
        )
        steps.append(
            "decision basis: virtual-goal A* with consistent potentials "
            "pops goals in non-decreasing true cost; distinct outputs "
            "dedup at (GOAL, output); ties drained then ranked "
            f"lexicographically; {verdict}"
        )

    result = KBestResult(
        input=text,
        fst=fst.name,
        outputs=outputs,
        complete=complete,
        expansions=expansions,
        settled_configurations=len(settled),
        budget=budget,
    )
    search_trace = SearchTrace(
        run_id=run_id, input=text, fst=fst.name, k=k, budget=budget,
        steps=tuple(steps), expansions=expansions, settled=len(settled),
        verdict=verdict,
    )
    return result, search_trace
