"""Propagation kernel.

Two propagators cooperate on a shared domain store:

* binary constraints run **AC-3** arc consistency over an explicit arc
  queue;
* each ``all_different`` group runs **Régin's algorithm**: a maximum
  bipartite matching (Hopcroft-Karp) detects Hall violations, and strongly
  connected components of the directed residual graph prune every edge that
  cannot belong to any matching that covers all variables. This is global
  matching/Hall reasoning, not pairwise deletion of assigned values.

Every pruning is appended to the trail together with a machine-readable
:class:`Reason`. Backtracking only replays the trail; the propagation queue
is local to each :func:`run_propagation` call and is therefore restored
implicitly when the search discards the branch frame.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .matching import hopcroft_karp
from .models import BinaryConstraint, CSPModel

Node = tuple[str, Any]  # ("v", variable) | ("u", value)


@dataclass(frozen=True)
class Reason:
    """Why a value was pruned."""

    constraint: str
    kind: str  # "binary_support" | "alldifferent_matching"
    detail: dict[str, Any]


class AllDifferentUnsatisfiable(Exception):
    """Raised when a Hall set proves the all-different group infeasible."""

    def __init__(
        self,
        group_index: int,
        variables: list[str],
        hall_vars: list[str],
        hall_values: list[int],
    ):
        self.group_index = group_index
        self.variables = variables
        self.hall_vars = hall_vars
        self.hall_values = hall_values
        super().__init__(
            f"all_different[{group_index}] Hall violation: "
            f"{len(hall_vars)} variables {hall_vars} share only "
            f"{len(hall_values)} values {sorted(hall_values)}"
        )


@dataclass
class PropagationStats:
    binary_revisions: int = 0
    alldifferent_runs: int = 0
    prunes: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)

    def record(self, variable: str, value: int, reason: Reason) -> None:
        self.steps.append(
            {
                "seq": len(self.steps),
                "variable": variable,
                "value": value,
                "constraint": reason.constraint,
                "kind": reason.kind,
                "detail": reason.detail,
            }
        )


@dataclass(frozen=True)
class Arc:
    constraint_id: str
    origin: str  # variable whose values need support
    target: str  # supporting variable
    constraint: BinaryConstraint
    reverse: bool


class Propagator:
    """Pre-indexed arc incidence and all-different groups for one model."""

    def __init__(self, model: CSPModel):
        self.model = model
        self.arcs: list[Arc] = []
        # A changed variable re-triggers every arc it participates in, in
        # either direction (it may have lost values or lost supporters).
        self.incident: dict[str, list[int]] = {var: [] for var in model.domains}
        for index, constraint in enumerate(model.binary_constraints):
            cid = f"bin[{index}]({constraint.left},{constraint.right})"
            forward_index = len(self.arcs)
            backward_index = forward_index + 1
            self.arcs.append(
                Arc(cid, constraint.left, constraint.right, constraint, False)
            )
            self.arcs.append(
                Arc(cid, constraint.right, constraint.left, constraint, True)
            )
            for variable in (constraint.left, constraint.right):
                self.incident[variable].extend((forward_index, backward_index))

        self.groups: list[list[str]] = [list(group) for group in model.all_different]
        self.var_groups: dict[str, list[int]] = {var: [] for var in model.domains}
        for group_index, group in enumerate(self.groups):
            for var in group:
                self.var_groups[var].append(group_index)


def run_propagation(
    propagator: Propagator,
    store,
    trail: list,
    stats: PropagationStats,
    changed: list[str] | None = None,
) -> None:
    """Propagate to fixpoint, pruning directly on ``store``.

    ``changed`` is the set of variables whose domain changed since the last
    fixpoint; ``None`` runs every propagator once (root propagation).
    """

    def do_prune(variable: str, value: int, reason: Reason) -> None:
        store.prune(variable, value, trail)
        stats.prunes += 1
        stats.record(variable, value, reason)

    arc_queue: deque[int] = deque()
    queued_arcs: set[int] = set()
    group_queue: deque[int] = deque()
    queued_groups: set[int] = set()

    def enqueue_variable(variable: str) -> None:
        for arc_index in propagator.incident.get(variable, ()):
            if arc_index not in queued_arcs:
                queued_arcs.add(arc_index)
                arc_queue.append(arc_index)
        for group_index in propagator.var_groups.get(variable, ()):
            if group_index not in queued_groups:
                queued_groups.add(group_index)
                group_queue.append(group_index)

    if changed is None:
        for arc_index in range(len(propagator.arcs)):
            arc_queue.append(arc_index)
        queued_arcs.update(range(len(propagator.arcs)))
        for group_index in range(len(propagator.groups)):
            group_queue.append(group_index)
        queued_groups.update(range(len(propagator.groups)))
    else:
        for variable in changed:
            enqueue_variable(variable)

    while arc_queue or group_queue:
        if arc_queue:
            arc_index = arc_queue.popleft()
            queued_arcs.discard(arc_index)
            affected = _revise_arc(propagator.arcs[arc_index], store, do_prune, stats)
        else:
            group_index = group_queue.popleft()
            queued_groups.discard(group_index)
            affected = _revise_alldifferent(
                propagator, group_index, store, do_prune, stats
            )
        for variable in affected:
            enqueue_variable(variable)


def _revise_arc(arc: Arc, store, do_prune, stats: PropagationStats) -> set[str]:
    affected: set[str] = set()
    origin_domain = store.domain(arc.origin)
    target_domain = store.domain(arc.target)
    relation = arc.constraint.relation
    for value in sorted(origin_domain):
        if arc.reverse:
            supported = any(relation.holds(other, value) for other in target_domain)
        else:
            supported = any(relation.holds(value, other) for other in target_domain)
        if not supported:
            do_prune(
                arc.origin,
                value,
                Reason(
                    constraint=arc.constraint_id,
                    kind="binary_support",
                    detail={
                        "origin": arc.origin,
                        "target": arc.target,
                        "lost_value": value,
                        "target_domain": sorted(target_domain),
                        "relation_kind": relation.kind.value,
                    },
                ),
            )
            affected.add(arc.origin)
    if affected:
        stats.binary_revisions += 1
    return affected


def _residual_graph(
    variables: list[str], store, matching: dict[str, int]
) -> tuple[set[Node], dict[Node, list[Node]], dict[int, str]]:
    """Build Régin's directed residual graph.

    Every admissible variable-value edge is oriented variable -> value; the
    unique matching edge incident to a matched value is oriented value ->
    variable. Matched and unmatched value nodes are both present; unmatched
    value nodes are sinks (they terminate augmenting paths).
    """
    value_to_var = {value: var for var, value in matching.items()}
    nodes: set[Node] = {("v", var) for var in variables}
    edges: dict[Node, list[Node]] = {}
    for var in variables:
        outgoing = [("u", value) for value in store.domain(var)]
        edges[("v", var)] = outgoing
        nodes.update(outgoing)
    for value, owner in value_to_var.items():
        edges[("u", value)] = [("v", owner)]
    for node in nodes:
        edges.setdefault(node, [])
    return nodes, edges, value_to_var


def _tarjan_sccs(nodes: set[Node], edges: dict[Node, list[Node]]) -> dict[Node, int]:
    """Iterative Tarjan SCC (avoids recursion limits on large graphs)."""
    index_counter = 0
    indices: dict[Node, int] = {}
    lowlink: dict[Node, int] = {}
    on_stack: set[Node] = set()
    stack: list[Node] = []
    component: dict[Node, int] = {}

    for root in nodes:
        if root in indices:
            continue
        indices[root] = lowlink[root] = index_counter
        index_counter += 1
        stack.append(root)
        on_stack.add(root)
        work: list[tuple[Node, Any]] = [(root, iter(edges[root]))]
        while work:
            node, iterator = work[-1]
            descended = False
            for nxt in iterator:
                if nxt not in indices:
                    indices[nxt] = lowlink[nxt] = index_counter
                    index_counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(edges[nxt])))
                    descended = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], indices[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == indices[node]:
                component_id = len(component)
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component[member] = component_id
                    if member == node:
                        break
    return component


def _reachability(
    seeds: list[Node], edges: dict[Node, list[Node]]
) -> tuple[set[Node], dict[Node, list[Node]]]:
    """Forward reachability from seeds; also returns the reversed edge map."""
    reverse: dict[Node, list[Node]] = {}
    for source, targets in edges.items():
        for target in targets:
            reverse.setdefault(target, []).append(source)
    seen = set(seeds)
    queue: deque[Node] = deque(seeds)
    while queue:
        node = queue.popleft()
        for nxt in edges.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen, reverse


def _hall_violation(
    variables: list[str], store, matching: dict[str, int]
) -> tuple[list[str], list[int]]:
    """Expose a Hall-violating subset via alternating reachability.

    Starting from unmatched variables, traverse non-matching edges to values
    and matching edges back; fewer reachable values than reached variables is
    the Hall violation.
    """
    value_to_var = {value: var for var, value in matching.items()}
    free = [var for var in variables if var not in matching]
    seen_vars: set[str] = set(free)
    seen_values: set[int] = set()
    queue: deque[Node] = deque(("v", var) for var in free)
    while queue:
        kind, token = queue.popleft()
        if kind == "v":
            for value in store.domain(token):
                if value not in seen_values:
                    seen_values.add(value)
                    queue.append(("u", value))
        else:
            owner = value_to_var.get(token)
            if owner is not None and owner not in seen_vars:
                seen_vars.add(owner)
                queue.append(("v", owner))
    return sorted(seen_vars), sorted(seen_values)


def _revise_alldifferent(
    propagator: Propagator,
    group_index: int,
    store,
    do_prune,
    stats: PropagationStats,
) -> set[str]:
    variables = propagator.groups[group_index]
    stats.alldifferent_runs += 1
    constraint_id = f"alldifferent[{group_index}]"

    snapshot = {var: frozenset(store.domain(var)) for var in variables}
    matching, _free = hopcroft_karp(snapshot)
    if len(matching) < len(variables):
        hall_vars, hall_values = _hall_violation(variables, store, matching)
        raise AllDifferentUnsatisfiable(
            group_index, variables, hall_vars, hall_values
        )

    nodes, edges, value_to_var = _residual_graph(variables, store, matching)
    component = _tarjan_sccs(nodes, edges)

    # Alternating reachability sets. A non-matching edge x -> d belongs to
    # some maximum matching when it lies on:
    #   * an alternating cycle           -> x, d in the same SCC, or
    #   * an even alternating path from a free (unmatched) variable
    #     -> x reachable forward from a free variable node (Rf), or
    #   * an even alternating path ending at a free value
    #     -> d can reach a free value node forward (Rb).
    free_var_nodes = [("v", var) for var in variables if var not in matching]
    domain_value_set = {
        value for var in variables for value in store.domain(var)
    }
    free_value_nodes = [
        ("u", value)
        for value in domain_value_set
        if value not in value_to_var
    ]
    reachable_forward, reverse_edges = _reachability(free_var_nodes, edges)
    can_reach_free_value, _ = _reachability(free_value_nodes, reverse_edges)

    affected: set[str] = set()
    for var in variables:
        var_node: Node = ("v", var)
        var_component = component[var_node]
        for value in sorted(store.domain(var)):
            value_node: Node = ("u", value)
            in_matching = value_to_var.get(value) == var
            in_cycle = component.get(value_node) == var_component
            on_path_from_free_var = var_node in reachable_forward
            on_path_to_free_value = value_node in can_reach_free_value
            if in_matching or in_cycle or on_path_from_free_var or on_path_to_free_value:
                continue
            do_prune(
                var,
                value,
                Reason(
                    constraint=constraint_id,
                    kind="alldifferent_matching",
                    detail={
                        "group": variables,
                        "removed_edge": [var, value],
                        "matching_owner": value_to_var.get(value),
                        "variable_component": var_component,
                        "value_component": component.get(value_node),
                        "rule": "edge belongs to no matching covering all "
                        "variables (no alternating cycle or even "
                        "alternating path from a free node)",
                    },
                ),
            )
            affected.add(var)
    return affected
