"""Soft constraints must never rescue a node that fails a hard constraint,
and equal-score feasible nodes must be ordered by node name.
"""

from app.analyzer import analyze
from app.models import (
    Affinity,
    AnalyzeRequest,
    CandidatePod,
    LabelSelectorRequirement,
    Node,
    NodeAffinity,
    NodeSelectorTerm,
    PreferredSchedulingTerm,
    Taint,
)


def _request(three_equal_nodes: bool = False):
    nodes = [
        Node(name="node-z", labels={"zone": "z"}, capacity={"cpu": "4", "memory": "8Gi"}),
        Node(name="node-a", labels={"zone": "a"}, capacity={"cpu": "4", "memory": "8Gi"}),
        Node(name="node-m", labels={"zone": "m"}, capacity={"cpu": "4", "memory": "8Gi"}),
    ]
    pod = CandidatePod(
        name="p",
        requests={"cpu": "1", "memory": "1Gi"},
        labels={"app": "p"},
    )
    return AnalyzeRequest(nodes=nodes, existing_pods=[], pod=pod)


def test_tie_break_by_node_name():
    result = analyze(_request())
    assert [r["node"] for r in result["ranking"]] == ["node-a", "node-m", "node-z"]
    assert result["selectedNode"] == "node-a"


def test_soft_preference_does_not_override_hard_taint():
    req = _request()
    # Hard taint on node-a (would win ties by name) with no toleration...
    req.nodes[1].taints.append(Taint(key="dedicated", value="x", effect="NoSchedule"))
    # ...and a strong soft preference for zone=a, which must be ignored for node-a.
    req.pod.affinity = Affinity(
        node_affinity=NodeAffinity(
            preferred_during_scheduling_ignored_during_execution=[
                PreferredSchedulingTerm(
                    weight=100,
                    preference=NodeSelectorTerm(
                        match_expressions=[
                            LabelSelectorRequirement(key="zone", operator="In", values=["a"])
                        ]
                    ),
                )
            ]
        )
    )
    result = analyze(req)
    entries = {e["node"]: e for e in result["nodes"]}
    assert entries["node-a"]["feasible"] is False
    assert entries["node-a"]["rejectionReason"] == "TaintNotTolerated"
    assert "score" not in entries["node-a"]
    assert result["selectedNode"] == "node-m"


def test_hard_spread_intersection_can_reject_all_nodes():
    # zone + rack DoNotSchedule constraints whose feasible sets do not intersect.
    from .conftest import parse_fixture

    result = analyze(parse_fixture("zone_skew_all_rejected.json"))
    assert result["selectedNode"] is None
    for entry in result["nodes"]:
        assert entry["rejectionReason"] == "TopologySpreadSkew"


def test_hard_spread_keeps_balancing_node_open():
    # With one constraint the currently-minimum domain always stays schedulable.
    from .conftest import parse_fixture

    result = analyze(parse_fixture("zone_skew.json"))
    assert result["selectedNode"] == "node-b1"
