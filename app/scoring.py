"""Soft-constraint scoring ("Score" phase).

Only nodes that passed every hard constraint are scored. Each scorer returns a
0-100 integer; the node score is the weighted average of *applicable* scorers
(applicability mirrors the scheduler plugins' enabled-when-configured rule).

Scorers (explicit subset, documented approximations — NOT upstream-identical):
- leastAllocated: free fraction after placing the pod, averaged over resources.
- preferredNodeAffinity: max satisfied preferred node term weight / 100.
- preferredPodAffinity: max satisfied preferred pod affinity weight / 100.
- taintPreference: -10 per untolerated PreferNoSchedule taint (floored at 0).
- topologySpread: ScheduleAnyway constraints, 100*(1 - skew/(maxSkew+1));
  DoNotSchedule constraints contribute to the same average using actual skew.
"""

from __future__ import annotations

from decimal import Decimal

from .filters import Context, node_label
from .models import Node
from .selectors import node_selector_term_matches, selector_matches

LEAST_ALLOCATED = "leastAllocated"
PREFERRED_NODE_AFFINITY = "preferredNodeAffinity"
PREFERRED_POD_AFFINITY = "preferredPodAffinity"
TAINT_PREFERENCE = "taintPreference"
TOPOLOGY_SPREAD = "topologySpread"

_ONE_HUNDRED = Decimal(100)


def _clamp_int(value: Decimal) -> int:
    rounded = int(value.to_integral_value(rounding="ROUND_HALF_UP"))
    return max(0, min(100, rounded))


# --------------------------------------------------------------------------- #
# Individual scorers
# --------------------------------------------------------------------------- #


def _candidate_requests(ctx: Context) -> dict[str, Decimal]:
    from .quantities import parse_resource_map

    return parse_resource_map(dict(ctx.request.pod.requests), "candidate pod")


def score_least_allocated(ctx: Context, node: Node) -> int:
    pod_requests = _candidate_requests(ctx)
    capacity = ctx.node_capacity[node.name]
    used = ctx.node_used[node.name]
    keys = set(capacity) | set(pod_requests)
    if not keys:
        return 100
    total = Decimal(0)
    for key in keys:
        cap = capacity.get(key, Decimal(0))
        if cap <= 0:
            fraction = Decimal(0)
        else:
            remaining = cap - used.get(key, Decimal(0)) - pod_requests.get(key, Decimal(0))
            fraction = max(Decimal(0), remaining) / cap
        total += fraction * _ONE_HUNDRED
    return _clamp_int(total / Decimal(len(keys)))


def score_preferred_node_affinity(ctx: Context, node: Node) -> int:
    affinity = ctx.request.pod.affinity
    terms = (
        affinity.node_affinity.preferred_during_scheduling_ignored_during_execution
        if affinity and affinity.node_affinity
        else []
    )
    return max(
        (term.weight for term in terms if node_selector_term_matches(term.preference, node.labels)),
        default=0,
    )


def score_preferred_pod_affinity(ctx: Context, node: Node) -> int:
    pod = ctx.request.pod
    affinity = pod.affinity
    if affinity is None:
        return 0
    best = 0
    if affinity.pod_affinity is not None:
        for weighted in affinity.pod_affinity.preferred_during_scheduling_ignored_during_execution:
            term = weighted.pod_affinity_term
            if term.topology_key not in node.labels:
                continue
            value = node.labels[term.topology_key]
            satisfied = any(
                node_label(ctx, p.node, term.topology_key) == value
                and selector_matches(term.label_selector, p.labels)
                for p in ctx.pods_in_namespaces(term.namespaces)
            )
            if satisfied:
                best = max(best, weighted.weight)
    if affinity.pod_anti_affinity is not None:
        for weighted in affinity.pod_anti_affinity.preferred_during_scheduling_ignored_during_execution:
            term = weighted.pod_affinity_term
            if term.topology_key not in node.labels:
                # No domain membership: the "avoid co-location" wish is vacuously met.
                best = max(best, weighted.weight)
                continue
            value = node.labels[term.topology_key]
            satisfied = not any(
                p.node != node.name
                and node_label(ctx, p.node, term.topology_key) == value
                and selector_matches(term.label_selector, p.labels)
                for p in ctx.pods_in_namespaces(term.namespaces)
            )
            if satisfied:
                best = max(best, weighted.weight)
    return best


def score_taint_preference(ctx: Context, node: Node) -> int:
    from .filters import taint_tolerated

    tolerations = ctx.request.pod.tolerations
    untolerated = [
        t
        for t in node.taints
        if t.effect == "PreferNoSchedule"
        and not any(taint_tolerated(t, tol) for tol in tolerations)
    ]
    return max(0, 100 - 10 * len(untolerated))


def score_topology_spread(ctx: Context, node: Node) -> int:
    constraints = ctx.request.pod.topology_spread_constraints
    if not constraints:
        return 0
    total = Decimal(0)
    for constraint in constraints:
        key = constraint.topology_key
        from .filters import _candidate_selector  # local import avoids a cycle

        selector = _candidate_selector(ctx, constraint)
        if key not in node.labels:
            score = 0
        else:
            counts: dict[str, int] = {
                n.labels[key]: 0 for n in ctx.request.nodes if key in n.labels
            }
            for existing in ctx.pods_in_namespaces(None):
                value = node_label(ctx, existing.node, key)
                if value is not None and selector_matches(selector, existing.labels):
                    counts[value] += 1
            skew = counts[node.labels[key]] + 1 - min(counts.values())
            score = max(
                0,
                min(
                    100,
                    int(
                        (
                            _ONE_HUNDRED
                            * (1 - Decimal(skew) / Decimal(constraint.max_skew + 1))
                        ).to_integral_value(rounding="ROUND_HALF_UP")
                    ),
                ),
            )
        total += score
    return _clamp_int(total / Decimal(len(constraints)))


# --------------------------------------------------------------------------- #
# Applicability + aggregation
# --------------------------------------------------------------------------- #


def _applicability(ctx: Context) -> dict[str, bool]:
    pod = ctx.request.pod
    affinity = pod.affinity
    return {
        LEAST_ALLOCATED: True,
        PREFERRED_NODE_AFFINITY: bool(
            affinity
            and affinity.node_affinity
            and affinity.node_affinity.preferred_during_scheduling_ignored_during_execution
        ),
        PREFERRED_POD_AFFINITY: bool(
            (
                affinity
                and affinity.pod_affinity
                and affinity.pod_affinity.preferred_during_scheduling_ignored_during_execution
            )
            or (
                affinity
                and affinity.pod_anti_affinity
                and affinity.pod_anti_affinity.preferred_during_scheduling_ignored_during_execution
            )
        ),
        TAINT_PREFERENCE: any(
            t.effect == "PreferNoSchedule" for n in ctx.request.nodes for t in n.taints
        ),
        TOPOLOGY_SPREAD: bool(pod.topology_spread_constraints),
    }


def score_node(ctx: Context, node: Node, weights: dict[str, float]) -> dict:
    """Return per-scorer scores, applicability flags, and weighted total."""
    raw = {
        LEAST_ALLOCATED: score_least_allocated(ctx, node),
        PREFERRED_NODE_AFFINITY: score_preferred_node_affinity(ctx, node),
        PREFERRED_POD_AFFINITY: score_preferred_pod_affinity(ctx, node),
        TAINT_PREFERENCE: score_taint_preference(ctx, node),
        TOPOLOGY_SPREAD: score_topology_spread(ctx, node),
    }
    applicable = _applicability(ctx)
    weight_sum = Decimal(0)
    weighted_sum = Decimal(0)
    components = []
    for name, score in raw.items():
        applies = applicable[name]
        weight = Decimal(str(weights[_internal_weight_key(name)])) if applies else Decimal(0)
        if applies and weight > 0:
            weighted_sum += weight * Decimal(score)
            weight_sum += weight
        components.append(
            {
                "scorer": name,
                "score": score,
                "applicable": applies,
                "weight": float(weight),
            }
        )
    total = weighted_sum / weight_sum if weight_sum > 0 else Decimal(0)
    return {
        "totalScore": float(total.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")),
        "components": components,
    }


def _internal_weight_key(scorer_name: str) -> str:
    return {
        LEAST_ALLOCATED: "least_allocated",
        PREFERRED_NODE_AFFINITY: "preferred_node_affinity",
        PREFERRED_POD_AFFINITY: "preferred_pod_affinity",
        TAINT_PREFERENCE: "taint_preference",
        TOPOLOGY_SPREAD: "topology_spread",
    }[scorer_name]
