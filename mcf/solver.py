"""Minimum-cost flow core solver.

Implements the successive shortest augmenting path algorithm with a
maintained potential function (Johnson-style reduced costs):

* Initial potentials ``h`` are shortest-path distances from the source in
  the original residual graph, computed with Bellman-Ford. The input is
  required to contain no negative-cost cycle *reachable from the source*;
  if one is found, :class:`NegativeCycleError` is raised.
* Every augmentation thereafter uses Dijkstra on the reduced cost
  ``c + h[u] - h[v]``, which is non-negative for every residual arc leaving
  a reached node, followed by a potential update
  ``h[v] += dist[v]`` (unreached nodes unchanged).

All capacities and costs are signed 64-bit integers; flows and the total
cost are returned as Python ``int`` (arbitrary precision, so the
accumulated cost can exceed int64 range).

Scale limits (small/medium inputs only):

    2 <= n <= 256 nodes, 0 <= m <= 2000 original edges,
    0 <= capacity <= 2**63 - 1,  -2**31 <= cost <= 2**31 - 1.

Numerical convention: all arithmetic is exact integer arithmetic, so the
only "tolerance" in play is a strict ``> 0`` test on residual capacity.
Floating point is never used on costs or flows.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

import numpy as np

# ---- Input / numeric limits (documented contract) --------------------------

MAX_N = 256
MAX_M = 2000
CAP_MAX = (1 << 63) - 1
COST_MIN = -(1 << 31)
COST_MAX = (1 << 31) - 1
# Safety guard on the number of augmentations; see README ("Failure states").
MAX_AUGMENTATIONS = 200_000

INF = (1 << 62)  # larger than any real path length (|cost|*n <= 2**39)


class MCFError(Exception):
    """Base class for all solver errors."""


class NegativeCycleError(MCFError):
    """A negative-cost cycle reachable from the source exists."""


class IterationLimitError(MCFError):
    """The augmentation cap was hit (normally means the input is out of scale)."""


@dataclass
class FlowResult:
    """Result of a minimum-cost flow computation.

    Attributes:
        flow: Total units sent from source to sink.
        cost: Total cost of the flow (Python int, arbitrary precision).
        edge_flows: Flow on each original input edge, in input order.
        potential: Final node potentials (shortest-path labels from the
            source in the final residual graph), as Python ints.
        sink_reachable: Whether the sink was reachable through the residual
            network at termination. False means max flow is 0.
        status: "optimal" (including the zero-flow / unreachable case).
    """

    flow: int
    cost: int
    edge_flows: list[int]
    potential: list[int]
    sink_reachable: bool
    status: str = field(default="optimal")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "flow": self.flow,
            "cost": self.cost,
            "sink_reachable": self.sink_reachable,
            "edge_flows": self.edge_flows,
            "potential": self.potential,
        }


def min_cost_max_flow(
    n: int,
    source: int,
    sink: int,
    edges: list[tuple[int, int, int, int]],
    max_flow_limit: int | None = None,
) -> FlowResult:
    """Compute a minimum-cost maximum (or limited) s-t flow.

    Args:
        n: Number of nodes, indexed ``0 .. n-1``.
        source / sink: Endpoint indices (must differ).
        edges: List of ``(u, v, capacity, cost)`` tuples. Capacity must be
            a non-negative int64; cost an int32-range integer. Parallel
            edges are supported. Self-loops are rejected by the JSON layer;
            the solver itself simply skips zero-capacity and s-t-irrelevant
            edges via reachability.
        max_flow_limit: If given, stop as soon as this many units have been
            sent (min-cost flow of that value, if feasible).

    Returns:
        FlowResult with the optimum.

    Raises:
        NegativeCycleError: a source-reachable negative cycle exists.
        IterationLimitError: augmentation cap exceeded.
    """
    if not (2 <= n <= MAX_N):
        raise MCFError(f"n must be in [2, {MAX_N}], got {n}")
    if not (0 <= source < n and 0 <= sink < n) or source == sink:
        raise MCFError("source and sink must be distinct valid node indices")
    if len(edges) > MAX_M:
        raise MCFError(f"too many edges: {len(edges)} > {MAX_M}")
    if max_flow_limit is not None and max_flow_limit < 0:
        raise MCFError("max_flow_limit must be non-negative")

    m = len(edges)

    # Residual graph arrays. Each original edge e creates forward arc 2e
    # (u->v, cap) and reverse arc 2e+1 (v->u, cap 0).
    arc_from = np.empty(2 * m, dtype=np.int64)
    arc_to = np.empty(2 * m, dtype=np.int64)
    arc_cost = np.empty(2 * m, dtype=np.int64)
    arc_cap = np.zeros(2 * m, dtype=np.int64)

    adjacency: list[list[int]] = [[] for _ in range(n)]
    for e, (u, v, cap, cost) in enumerate(edges):
        if not (0 <= u < n and 0 <= v < n):
            raise MCFError(f"edge {e}: endpoint out of range")
        if not (0 <= cap <= CAP_MAX):
            raise MCFError(f"edge {e}: capacity out of int64 non-negative range")
        if not (COST_MIN <= cost <= COST_MAX):
            raise MCFError(f"edge {e}: cost outside int32 range")
        f, r = 2 * e, 2 * e + 1
        arc_from[f], arc_to[f], arc_cost[f], arc_cap[f] = u, v, cost, cap
        arc_from[r], arc_to[r], arc_cost[r] = v, u, -cost
        # Self loops can never carry useful s-t flow; the JSON layer
        # rejects them outright, and here they are kept out of the
        # adjacency structure (their residual capacity is never touched,
        # so the reported flow stays 0).
        if u != v:
            adjacency[u].append(f)
            adjacency[v].append(r)

    # ---- Initial potentials: Bellman-Ford from source, vectorized ----------
    h, bf_reachable = _initial_potential(
        n, source, arc_from, arc_to, arc_cost, arc_cap, adjacency
    )

    flow = 0
    cost_total = 0

    if bf_reachable[sink]:
        for _ in range(MAX_AUGMENTATIONS + 1):
            if max_flow_limit is not None and flow >= max_flow_limit:
                break

            dist, parent_arc = _dijkstra(
                n, source, h, arc_from, arc_to, arc_cost, arc_cap, adjacency
            )

            if dist[sink] >= INF:
                break  # sink unreachable in the residual network -> max flow

            # Potential update for every reached node.
            reached = dist < INF
            h = h.copy()
            h[reached] += dist[reached]

            # Trace the s->t path and find its bottleneck capacity.
            path_arcs: list[int] = []
            x = sink
            while x != source:
                a = int(parent_arc[x])
                path_arcs.append(a)
                x = int(arc_from[a])
            path_arcs.reverse()

            add = min(int(arc_cap[a]) for a in path_arcs)
            if max_flow_limit is not None:
                add = min(add, max_flow_limit - flow)
            if add <= 0:
                raise MCFError("internal error: zero-capacity augmentation path")

            # Augment. Flow on original edge e is:
            #   initial_cap(e) - residual_cap(forward arc 2e)
            for a in path_arcs:
                arc_cap[a] -= add
                arc_cap[a ^ 1] += add
            flow += add
            # Path cost in original costs equals h[t] after the update
            # (h[s] stays 0); use direct summation for an exact check value.
            cost_total += add * sum(int(arc_cost[a]) for a in path_arcs)
        else:
            raise IterationLimitError(
                f"exceeded {MAX_AUGMENTATIONS} augmentations"
            )

    edge_flows = [0] * m
    for e in range(m):
        cap0 = int(edges[e][2])
        edge_flows[e] = cap0 - int(arc_cap[2 * e])
        # A forward arc's residual drop equals flow pushed (net of cancellations).
        if not (0 <= edge_flows[e] <= cap0):
            raise MCFError("internal error: edge flow outside capacity bounds")

    sink_reachable = bool(
        _reachable(n, source, sink, arc_from, arc_to, arc_cap, adjacency)
    )

    return FlowResult(
        flow=int(flow),
        cost=int(cost_total),
        edge_flows=edge_flows,
        potential=[int(x) for x in h],
        sink_reachable=sink_reachable,
        status="optimal",
    )


def _initial_potential(
    n, source, arc_from, arc_to, arc_cost, arc_cap, adjacency
) -> tuple[np.ndarray, np.ndarray]:
    """Bellman-Ford shortest distances from ``source`` over residual arcs.

    Only arcs with residual capacity > 0 participate. Relaxation is
    vectorized with NumPy (in-place, one pass per round; the classic
    n-th-round update test still characterizes negative cycles exactly).

    Returns ``(dist, reachable)`` where ``reachable[v]`` marks nodes reached
    by the last full relaxation pass (used to identify nodes that are still
    relaxing in round n, i.e. affected by / able to reach a negative cycle).
    """
    dist = np.full(n, INF, dtype=np.int64)
    dist[source] = 0

    # Only initially usable arcs matter; all reverse arcs start at cap 0.
    usable = arc_cap > 0
    u_idx = arc_from[usable]
    v_idx = arc_to[usable]
    w = arc_cost[usable]

    improved = np.zeros(n, dtype=bool)
    for _ in range(n):
        # Fancy indexing snapshots dist[u_idx], so each round is a
        # synchronous Bellman-Ford relaxation pass.
        cand = dist[u_idx] + w
        valid = dist[u_idx] < INF
        better = valid & (cand < dist[v_idx])
        if not np.any(better):
            return dist, (dist < INF)
        # Scatter-min of candidate distances (parallel arcs handled).
        best = np.full(n, INF, dtype=np.int64)
        np.minimum.at(best, v_idx[better], cand[better])
        improved = best < dist
        dist = np.where(improved, best, dist)

    # An improvement in the n-th pass implies an s-reachable negative cycle.
    affected = int(np.argmax(improved)) if np.any(improved) else -1
    raise NegativeCycleError(
        f"negative-cost cycle reachable from source (node {affected} still "
        "relaxing in Bellman-Ford round n)"
    )


def _dijkstra(
    n, source, h, arc_from, arc_to, arc_cost, arc_cap, adjacency
) -> tuple[np.ndarray, np.ndarray]:
    """Dijkstra over the whole component reachable from ``source`` using
    non-negative reduced costs.

    Reduced cost of arc a (u->v) is cost[a] + h[u] - h[v]. The potential
    invariant guarantees this is >= 0 for arcs leaving reached nodes; a
    strict violation indicates an internal invariant failure.
    """
    dist = np.full(n, INF, dtype=np.int64)
    parent_arc = np.full(n, -1, dtype=np.int64)
    dist[source] = 0
    done = np.zeros(n, dtype=bool)
    heap: list[tuple[int, int]] = [(0, source)]

    h_local = h  # numpy array
    while heap:
        d, u = heapq.heappop(heap)
        if done[u]:
            continue
        done[u] = True
        # NOTE: no early exit at the sink. Potentials are updated for
        # every reached node afterwards; stopping early would leave
        # reachable nodes with tentative (unfinalized) distances, and
        # reduced-cost inequalities on arcs out of those nodes could be
        # violated in the next augmentation round.
        hu = int(h_local[u])
        for a in adjacency[u]:
            if arc_cap[a] <= 0:
                continue
            v = int(arc_to[a])
            if done[v]:
                continue
            rc = int(arc_cost[a]) + hu - int(h_local[v])
            if rc < 0:
                raise MCFError(
                    f"internal error: negative reduced cost {rc} on arc {a} "
                    "(potential invariant violated)"
                )
            nd = d + rc
            if nd < int(dist[v]):
                dist[v] = nd
                parent_arc[v] = a
                heapq.heappush(heap, (nd, v))

    return dist, parent_arc


def _reachable(n, source, sink, arc_from, arc_to, arc_cap, adjacency) -> bool:
    """BFS reachability over positive-capacity residual arcs."""
    seen = np.zeros(n, dtype=bool)
    seen[source] = True
    stack = [source]
    while stack:
        u = stack.pop()
        if u == sink:
            return True
        for a in adjacency[u]:
            if arc_cap[a] > 0:
                v = int(arc_to[a])
                if not seen[v]:
                    seen[v] = True
                    stack.append(v)
    return bool(seen[sink])
