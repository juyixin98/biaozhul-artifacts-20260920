"""Search kernel: BFS, uniform-cost and A* over grounded STRIPS problems.

Result contract (:class:`SearchResult`):

* ``solved``     - a plan was found; ``plan`` is populated and the caller
                   must still re-verify it with the independent executor.
* ``unsolvable`` - the reachable space was exhausted; the goal is provably
                   unreachable from the initial state.
* ``unknown``    - a bound was hit before the question was settled.
                   ``reason`` names the bound; no yes/no claim is made.

State de-duplication is over the canonical state itself (frozenset of
atoms): one ``best_g`` entry per distinct state, so action cycles cannot
generate duplicate work. Cost-optimality is promised only when the
algorithm/heuristic combination is admissible (``optimal_guarantee``).
BFS promises fewest *steps*, not least action cost.
"""

from __future__ import annotations

import heapq
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .encoding import state_hash
from .grounding import GroundProblem
from .heuristics import HEURISTICS, INF
from .model import GroundAction, State, atom_text
from .semantics import applicable, goal_satisfied

SOLVED = "solved"
UNSOLVABLE = "unsolvable"
UNKNOWN = "unknown"

NODE_LIMIT = "node_limit"
FRONTIER_LIMIT = "frontier_limit"
DEPTH_LIMIT = "depth_limit"
TIME_LIMIT = "time_limit"

BFS = "bfs"
UCS = "ucs"
ASTAR = "astar"


@dataclass(frozen=True)
class SearchConfig:
    algorithm: str = ASTAR
    heuristic: str = "h_max"
    max_expansions: int = 100_000
    max_frontier: int = 100_000
    max_depth: Optional[int] = None       # path length in steps, not cost
    time_limit_seconds: float = 30.0
    trace_limit: int = 500                # expanded states kept for evidence


@dataclass
class TraceEntry:
    seq: int
    depth: int
    g: int
    h: Optional[float]
    f: Optional[float]
    action: Optional[str]
    state_hash: str
    state_text: tuple[str, ...]


@dataclass
class SearchResult:
    status: str
    plan: tuple[GroundAction, ...] = field(default_factory=tuple)
    cost: Optional[int] = None
    path_length: Optional[int] = None
    expanded: int = 0
    generated: int = 0
    reopened: int = 0
    pruned_dead_end: int = 0
    frontier_peak: int = 0
    optimal_guarantee: bool = False
    reason: Optional[str] = None
    bounds: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    trace: list[TraceEntry] = field(default_factory=list)
    final_frontier_sample: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "plan": [
                {"name": a.schema_name, "args": list(a.args), "cost": a.cost}
                for a in self.plan
            ],
            "cost": self.cost,
            "path_length": self.path_length,
            "expanded": self.expanded,
            "generated": self.generated,
            "reopened": self.reopened,
            "pruned_dead_end": self.pruned_dead_end,
            "frontier_peak": self.frontier_peak,
            "optimal_guarantee": self.optimal_guarantee,
            "reason": self.reason,
            "bounds": self.bounds,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "trace": [_trace_to_dict(t) for t in self.trace],
        }


def _trace_to_dict(t: TraceEntry) -> dict:
    return {
        "seq": t.seq,
        "depth": t.depth,
        "g": t.g,
        "h": None if t.h is None or t.h == INF else t.h,
        "f": None if t.f is None or t.f == INF else t.f,
        "action": t.action,
        "state_hash": t.state_hash,
        "state": list(t.state_text),
    }


def successor(state: State, action: GroundAction) -> State:
    """Single-predecessor, fixed-order transition used by every algorithm."""
    return frozenset(
        (set(state) - set(action.del_effects)) | set(action.add_effects)
    )


def search(ground_problem: GroundProblem, config: SearchConfig) -> SearchResult:
    if config.algorithm == BFS:
        return _bfs(ground_problem, config)
    if config.algorithm in (UCS, ASTAR):
        return _best_first(ground_problem, config)
    raise ValueError(f"unknown algorithm {config.algorithm!r}")


# --------------------------------------------------------------------------- #
# Best-first (uniform-cost when h == zero, A* otherwise)
# --------------------------------------------------------------------------- #

def _best_first(gp: GroundProblem, config: SearchConfig) -> SearchResult:
    heuristic = HEURISTICS[config.heuristic](gp)
    guarantee = config.algorithm == UCS or (
        config.algorithm == ASTAR and heuristic.admissible
    )
    result = SearchResult(
        status=UNKNOWN,
        optimal_guarantee=guarantee,
        bounds=_bounds(config),
    )
    started = time.monotonic()
    deadline = started + config.time_limit_seconds

    start_state = gp.initial
    counter = 0
    heap: list = []
    # entry: (f, tie, g, depth, state, generating_action)
    heapq.heappush(heap, (_priority(heuristic.value(start_state), 0),
                          counter, 0, 0, start_state, None))
    best_g: dict[State, int] = {start_state: 0}
    parent: dict[State, tuple[State, GroundAction]] = {}
    queued: set[State] = {start_state}
    truncated = False

    while heap:
        if result.expanded >= config.max_expansions:
            return _unknown(result, NODE_LIMIT, heap, started, heuristic)
        if time.monotonic() >= deadline:
            return _unknown(result, TIME_LIMIT, heap, started, heuristic)

        _, _, g, depth, state, gen_action = heapq.heappop(heap)
        if state not in queued or g != best_g.get(state):
            continue  # superseded duplicate entry
        queued.discard(state)

        result.expanded += 1
        h = heuristic.value(state)
        _record_trace(result, config, state, g, depth, h, gen_action)

        if goal_satisfied(state, gp.goal_pos, gp.goal_neg):
            return _solved(result, state, parent, g, depth, started)

        if config.max_depth is not None and depth >= config.max_depth:
            truncated = True
            continue

        for action in gp.actions:
            if not applicable(state, action):
                continue
            result.generated += 1
            next_state = successor(state, action)
            ng = g + action.cost
            if ng >= best_g.get(next_state, float("inf")):
                continue
            if next_state in best_g:
                result.reopened += 1
            best_g[next_state] = ng
            parent[next_state] = (state, action)
            nh = heuristic.value(next_state)
            if nh == INF:
                # Heuristic dead-end opinion: keep the node (completeness)
                # but order it behind every finite-f node.
                result.pruned_dead_end += 1
            counter += 1
            if len(heap) >= config.max_frontier:
                return _unknown(result, FRONTIER_LIMIT, heap, started, heuristic)
            heapq.heappush(
                heap,
                (_priority(nh, ng), counter, ng, depth + 1, next_state, action),
            )
            queued.add(next_state)
            result.frontier_peak = max(result.frontier_peak, len(heap))

    if truncated:
        return _unknown(result, DEPTH_LIMIT, heap, started, heuristic)
    result.status = UNSOLVABLE
    result.elapsed_seconds = time.monotonic() - started
    return result


# --------------------------------------------------------------------------- #
# Breadth-first: fewest-steps optimal; action costs are summed, not minimised
# --------------------------------------------------------------------------- #

def _bfs(gp: GroundProblem, config: SearchConfig) -> SearchResult:
    result = SearchResult(
        status=UNKNOWN,
        optimal_guarantee=True,
        bounds=_bounds(config),
    )
    started = time.monotonic()
    deadline = started + config.time_limit_seconds
    start_state = gp.initial

    if goal_satisfied(start_state, gp.goal_pos, gp.goal_neg):
        result.expanded += 1
        _record_trace(result, config, start_state, 0, 0, 0, None)
        return _solved(result, start_state, {}, 0, 0, started)

    queue: deque[tuple[State, int, int]] = deque([(start_state, 0, 0)])
    visited: set[State] = {start_state}
    parent: dict[State, tuple[State, GroundAction]] = {}
    truncated = False

    while queue:
        if result.expanded >= config.max_expansions:
            return _unknown(result, NODE_LIMIT, queue, started, None)
        if time.monotonic() >= deadline:
            return _unknown(result, TIME_LIMIT, queue, started, None)

        state, g, depth = queue.popleft()
        result.expanded += 1
        gen_action = parent[state][1] if state in parent else None
        _record_trace(result, config, state, g, depth, None, gen_action)

        if config.max_depth is not None and depth >= config.max_depth:
            truncated = True
            continue

        for action in gp.actions:
            if not applicable(state, action):
                continue
            result.generated += 1
            next_state = successor(state, action)
            if next_state in visited:
                continue
            visited.add(next_state)
            parent[next_state] = (state, action)
            ng = g + action.cost

            if goal_satisfied(next_state, gp.goal_pos, gp.goal_neg):
                result.expanded += 1
                _record_trace(result, config, next_state, ng, depth + 1, 0, action)
                return _solved(result, next_state, parent, ng, depth + 1, started)

            if len(queue) >= config.max_frontier:
                return _unknown(result, FRONTIER_LIMIT, queue, started, None)
            queue.append((next_state, ng, depth + 1))
            result.frontier_peak = max(result.frontier_peak, len(queue))

    if truncated:
        return _unknown(result, DEPTH_LIMIT, queue, started, None)
    result.status = UNSOLVABLE
    result.elapsed_seconds = time.monotonic() - started
    return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _bounds(config: SearchConfig) -> dict:
    return {
        "max_expansions": config.max_expansions,
        "max_frontier": config.max_frontier,
        "max_depth": config.max_depth,
        "time_limit_seconds": config.time_limit_seconds,
    }


def _priority(h, g: int) -> float:
    return g if h == INF else g + h


def _record_trace(result, config, state: State, g: int, depth: int,
                  h, action) -> None:
    if len(result.trace) >= config.trace_limit:
        return
    result.trace.append(
        TraceEntry(
            seq=result.expanded,
            depth=depth,
            g=g,
            h=h,
            f=None if h is None else _priority(h, g),
            action=None if action is None else action.signature,
            state_hash=state_hash(state),
            state_text=tuple(atom_text(a) for a in sorted(state)),
        )
    )


def _solved(result, goal_state, parent, g, depth, started) -> SearchResult:
    plan: list[GroundAction] = []
    cursor = goal_state
    while cursor in parent:
        prev, action = parent[cursor]
        plan.append(action)
        cursor = prev
    plan.reverse()
    result.status = SOLVED
    result.plan = tuple(plan)
    result.cost = g
    result.path_length = depth
    result.elapsed_seconds = time.monotonic() - started
    return result


def _unknown(result, reason, frontier, started, heuristic) -> SearchResult:
    result.status = UNKNOWN
    result.reason = reason
    result.elapsed_seconds = time.monotonic() - started

    sample: list[dict] = []
    for item in list(frontier)[:10]:
        _, _, g, depth, st, _act = item
        h_val = heuristic.value(st) if heuristic is not None else None
        sample.append({
            "g": g,
            "depth": depth,
            "h": None if h_val is None or h_val == INF else h_val,
            "state_hash": state_hash(st),
        })
    result.final_frontier_sample = sample
    return result
