"""Orchestration: build context, filter hard constraints, then score feasible nodes."""

from __future__ import annotations

from .filters import Context, FilterError, filter_node
from .models import AnalyzeRequest
from .scoring import score_node

SCOPE = "offline-kubernetes-scheduler-subset/v1"
FEATURES = [
    "resource-fit-requests",
    "taints-tolerations",
    "node-selector",
    "required-node-affinity",
    "preferred-node-affinity",
    "required-pod-affinity",
    "preferred-pod-affinity",
    "required-pod-anti-affinity",
    "preferred-pod-anti-affinity",
    "topology-spread-hard",
    "topology-spread-soft",
    "hmac-sha256-response-signing",
]


def analyze(request: AnalyzeRequest) -> dict:
    ctx = Context.build(request)
    weights = _weights(request)

    nodes_out = []
    feasible_nodes = []
    for node in request.nodes:
        feasible, reason, evidence = filter_node(ctx, node)
        entry = {
            "node": node.name,
            "feasible": feasible,
            "rejectionReason": None if feasible else reason,
            "evidence": None if feasible else evidence,
        }
        if feasible:
            scored = score_node(ctx, node, weights)
            entry["score"] = scored
            feasible_nodes.append((node.name, scored["totalScore"]))
        nodes_out.append(entry)

    # Tie-break: higher score first, then node name ascending.
    feasible_nodes.sort(key=lambda item: (-item[1], item[0]))
    ranked = [
        {"node": name, "score": score, "rank": index + 1}
        for index, (name, score) in enumerate(feasible_nodes)
    ]

    return {
        "scope": SCOPE,
        "implementedFeatures": FEATURES,
        "notImplemented": [
            "Full Kubernetes default scheduler parity",
            "Live cluster connection / real-time utilization",
            "Volume binding, pod disruption, priority/preemption, device plugins",
        ],
        "pod": request.pod.name,
        "namespace": request.pod.namespace,
        "nodes": nodes_out,
        "ranking": ranked,
        "selectedNode": ranked[0]["node"] if ranked else None,
    }


def _weights(request: AnalyzeRequest) -> dict[str, float]:
    overrides = request.scoring_weights
    defaults = {
        "least_allocated": 1.0,
        "preferred_node_affinity": 1.0,
        "preferred_pod_affinity": 1.0,
        "taint_preference": 1.0,
        "topology_spread": 1.0,
    }
    if overrides is not None:
        for key in defaults:
            defaults[key] = getattr(overrides, key)
    return defaults
