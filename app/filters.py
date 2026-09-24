"""Hard constraint filtering ("Filter" phase).

A node is rejected as soon as one hard constraint fails; the first failure in
the fixed order below is reported. Soft constraints (preferences, PreferNoSchedule
taints, ScheduleAnyway spread) are deliberately NOT evaluated here.

Hard constraints implemented (explicit subset):
  1. resource fit (requests vs allocatable remaining, summed from existing pods)
  2. taints vs tolerations (NoSchedule / NoExecute only)
  3. pod.nodeSelector
  4. required node affinity
  5. required pod affinity
  6. required pod anti-affinity
  7. DoNotSchedule topology spread constraints
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import AnalyzeRequest, ExistingPod, Node, Taint, Toleration
from .quantities import QuantityError, parse_resource_map
from .selectors import node_selector_term_matches, selector_matches

REASON_INSUFFICIENT_RESOURCES = "InsufficientResources"
REASON_TAINT_NOT_TOLERATED = "TaintNotTolerated"
REASON_NODE_SELECTOR_NOT_MATCH = "NodeSelectorNotMatch"
REASON_NODE_AFFINITY_NOT_MATCH = "NodeAffinityNotMatch"
REASON_POD_AFFINITY_CONFLICT = "PodAffinityConflict"
REASON_POD_ANTI_AFFINITY_CONFLICT = "PodAntiAffinityConflict"
REASON_TOPOLOGY_SPREAD_SKEW = "TopologySpreadSkew"
REASON_MISSING_TOPOLOGY_LABEL = "MissingTopologyLabel"


class FilterError(ValueError):
    """Bad request data discovered while filtering (e.g. unparseable quantity)."""


@dataclass
class Context:
    request: AnalyzeRequest
    node_capacity: dict[str, dict[str, Decimal]]
    node_used: dict[str, dict[str, Decimal]]

    @classmethod
    def build(cls, request: AnalyzeRequest) -> "Context":
        capacity: dict[str, dict[str, Decimal]] = {}
        for node in request.nodes:
            source = node.allocatable if node.allocatable is not None else node.capacity
            try:
                capacity[node.name] = parse_resource_map(dict(source), f"node {node.name}")
            except QuantityError as exc:
                raise FilterError(str(exc)) from None
        used: dict[str, dict[str, Decimal]] = {n.name: {} for n in request.nodes}
        for pod in request.existing_pods:
            try:
                reqs = parse_resource_map(dict(pod.requests), f"pod {pod.name}")
            except QuantityError as exc:
                raise FilterError(str(exc)) from None
            bucket = used[pod.node]
            for key, value in reqs.items():
                bucket[key] = bucket.get(key, Decimal(0)) + value
        return cls(request=request, node_capacity=capacity, node_used=used)

    def pods_in_namespaces(self, namespaces: list[str] | None) -> list[ExistingPod]:
        ns = set(namespaces) if namespaces is not None else {self.request.pod.namespace}
        return [p for p in self.request.existing_pods if p.namespace in ns]


# --------------------------------------------------------------------------- #
# Individual hard constraints
# --------------------------------------------------------------------------- #


def taint_tolerated(taint: Taint, toleration: Toleration) -> bool:
    # Empty key + Exists tolerates every taint (including empty key).
    if not toleration.key:
        return toleration.operator == "Exists" and _effect_matches(taint, toleration)
    if toleration.key != taint.key:
        return False
    if toleration.operator == "Equal" and toleration.value != taint.value:
        return False
    # Exists with a key tolerates any value of that key.
    return _effect_matches(taint, toleration)


def _effect_matches(taint: Taint, toleration: Toleration) -> bool:
    return not toleration.effect or toleration.effect == taint.effect


def _check_resources(ctx: Context, node: Node) -> tuple[bool, dict | None]:
    try:
        requested = parse_resource_map(dict(ctx.request.pod.requests), "candidate pod")
    except QuantityError as exc:
        raise FilterError(str(exc)) from None
    capacity = ctx.node_capacity[node.name]
    used = ctx.node_used[node.name]
    shortfall: dict[str, str] = {}
    for key, need in requested.items():
        available = capacity.get(key, Decimal(0)) - used.get(key, Decimal(0))
        if need > available:
            shortfall[key] = {
                "requested": _num(need),
                "allocatable": _num(capacity.get(key, Decimal(0))),
                "alreadyUsed": _num(used.get(key, Decimal(0))),
                "available": _num(available),
            }
    if shortfall:
        return False, shortfall
    return True, None


def _check_taints(node: Node, tolerations: list[Toleration]) -> tuple[bool, dict | None]:
    hard_effects = ("NoSchedule", "NoExecute")
    for taint in node.taints:
        if taint.effect not in hard_effects:
            continue
        if not any(taint_tolerated(taint, tol) for tol in tolerations):
            return False, {
                "taint": {"key": taint.key, "value": taint.value, "effect": taint.effect}
            }
    return True, None


def _check_node_selector(node: Node, selector: dict[str, str]) -> tuple[bool, dict | None]:
    mismatched = {
        k: {"required": v, "actual": node.labels.get(k)}
        for k, v in selector.items()
        if node.labels.get(k) != v
    }
    return (not mismatched, mismatched or None)


def _check_node_affinity(ctx: Context, node: Node) -> tuple[bool, dict | None]:
    affinity = ctx.request.pod.affinity
    if affinity is None or affinity.node_affinity is None:
        return True, None
    terms = affinity.node_affinity.required_during_scheduling_ignored_during_execution
    if not terms:
        return True, None
    for term in terms:  # OR over terms
        if node_selector_term_matches(term, node.labels):
            return True, None
    return False, {"failedTerms": len(terms)}


def _topology_domain_pods(
    ctx: Context, node: Node, topology_key: str, namespaces: list[str] | None
) -> tuple[list[ExistingPod] | None, str | None]:
    """Existing pods in the same topology domain as ``node``.

    Pod affinity: pods on the candidate node itself count (same domain).
    Anti-affinity callers filter out pods on the candidate node themselves,
    since existing pods on the very same node cannot conflict until the
    candidate is placed.
    """
    value = node.labels.get(topology_key)
    if value is None:
        return None, None
    return (
        [
            p
            for p in ctx.pods_in_namespaces(namespaces)
            if node_label(ctx, p.node, topology_key) == value
        ],
        value,
    )


def node_label(ctx: Context, node_name: str, key: str) -> str | None:
    for n in ctx.request.nodes:
        if n.name == node_name:
            return n.labels.get(key)
    return None


def _check_pod_affinity(ctx: Context, node: Node) -> tuple[bool, str | None, dict | None]:
    pod = ctx.request.pod
    affinity = pod.affinity
    if affinity is None or affinity.pod_affinity is None:
        return True, None, None
    for term in affinity.pod_affinity.required_during_scheduling_ignored_during_execution:
        if term.topology_key not in node.labels:
            return False, REASON_MISSING_TOPOLOGY_LABEL, {
                "topologyKey": term.topology_key
            }
        domain_pods, _ = _topology_domain_pods(ctx, node, term.topology_key, term.namespaces)
        matched = any(selector_matches(term.label_selector, p.labels) for p in domain_pods or [])
        if not matched:
            return False, REASON_POD_AFFINITY_CONFLICT, {"topologyKey": term.topology_key}
    return True, None, None


def _check_pod_anti_affinity(ctx: Context, node: Node) -> tuple[bool, dict | None]:
    pod = ctx.request.pod
    affinity = pod.affinity
    if affinity is None or affinity.pod_anti_affinity is None:
        return True, None
    for term in affinity.pod_anti_affinity.required_during_scheduling_ignored_during_execution:
        if term.topology_key not in node.labels:
            # Documented deviation from upstream: a node missing the topology
            # label cannot share a domain with any other labeled node, so it
            # passes anti-affinity (rather than being rejected upstream-style).
            continue
        domain_pods, _ = _topology_domain_pods(ctx, node, term.topology_key, term.namespaces)
        for other in domain_pods or []:
            # Pods on the candidate node itself share the same topology domain
            # (the domain is defined by the node's labels), so they conflict too.
            if selector_matches(term.label_selector, other.labels):
                return False, {
                    "topologyKey": term.topology_key,
                    "conflictingPod": other.name,
                }
    return True, None


def _spread_domain_counts(
    ctx: Context, topology_key: str, selector
) -> dict[str, int]:
    """Matching-pod counts per topology domain (0-filled for eligible nodes).

    Nodes missing the topology label are not part of any domain (upstream
    excludes them); they are rejected individually via MissingTopologyLabel.
    """
    counts: dict[str, int] = {
        n.labels[topology_key]: 0 for n in ctx.request.nodes if topology_key in n.labels
    }
    for pod in ctx.pods_in_namespaces(None):
        value = node_label(ctx, pod.node, topology_key)
        if value is not None and selector_matches(selector, pod.labels):
            counts[value] += 1
    return counts


def _candidate_selector(ctx: Context, constraint):
    return constraint.label_selector or _own_labels_selector(ctx)


def _own_labels_selector(ctx: Context):
    from .models import LabelSelector

    return LabelSelector(match_labels=dict(ctx.request.pod.labels))


def _check_topology_spread(ctx: Context, node: Node) -> tuple[bool, str | None, dict | None]:
    for constraint in ctx.request.pod.topology_spread_constraints:
        if constraint.when_unsatisfiable != "DoNotSchedule":
            continue
        key = constraint.topology_key
        if key not in node.labels:
            return False, REASON_MISSING_TOPOLOGY_LABEL, {"topologyKey": key}
        counts = _spread_domain_counts(ctx, key, _candidate_selector(ctx, constraint))
        skew = counts[node.labels[key]] + 1 - min(counts.values())
        if skew > constraint.max_skew:
            return False, REASON_TOPOLOGY_SPREAD_SKEW, {
                "topologyKey": key,
                "maxSkew": constraint.max_skew,
                "actualSkew": skew,
                "domain": node.labels[key],
                "domainCounts": counts,
            }
    return True, None, None


def _num(value: Decimal) -> float:
    return float(value)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def filter_node(ctx: Context, node: Node) -> tuple[bool, str, dict | None]:
    """Return (feasible, reason_code, evidence). reason is ``""`` when feasible."""
    pod = ctx.request.pod

    ok, evidence = _check_resources(ctx, node)
    if not ok:
        return False, REASON_INSUFFICIENT_RESOURCES, {"shortfall": evidence}

    ok, evidence = _check_taints(node, pod.tolerations)
    if not ok:
        return False, REASON_TAINT_NOT_TOLERATED, evidence

    ok, evidence = _check_node_selector(node, pod.node_selector)
    if not ok:
        return False, REASON_NODE_SELECTOR_NOT_MATCH, {"mismatched": evidence}

    ok, evidence = _check_node_affinity(ctx, node)
    if not ok:
        return False, REASON_NODE_AFFINITY_NOT_MATCH, evidence

    ok, reason, evidence = _check_pod_affinity(ctx, node)
    if not ok:
        return False, reason, evidence

    ok, evidence = _check_pod_anti_affinity(ctx, node)
    if not ok:
        return False, REASON_POD_ANTI_AFFINITY_CONFLICT, evidence

    ok, reason, evidence = _check_topology_spread(ctx, node)
    if not ok:
        return False, reason, evidence

    return True, "", None
