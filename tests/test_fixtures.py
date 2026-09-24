"""Each supplied fixture is verified against its declared expectations."""

from __future__ import annotations

from app.analyzer import analyze

from .conftest import load_fixture, parse_fixture


def _run(name: str) -> dict:
    fixture = load_fixture(name)
    result = analyze(parse_fixture(name))
    return fixture, result


def _by_node(result: dict) -> dict:
    return {entry["node"]: entry for entry in result["nodes"]}


def test_fixture_insufficient_resources():
    fixture, result = _run("insufficient_resources.json")
    expected = fixture["expectations"]
    nodes = _by_node(result)
    assert nodes["node-a"]["rejectionReason"] == expected["rejected"]["node-a"]
    assert nodes["node-a"]["evidence"]["shortfall"]["cpu"]["requested"] == 1.0
    assert nodes["node-a"]["evidence"]["shortfall"]["cpu"]["available"] == 0.5
    assert nodes["node-b"]["feasible"] is True
    assert result["selectedNode"] == "node-b"


def test_fixture_anti_affinity_conflict():
    fixture, result = _run("anti_affinity_conflict.json")
    nodes = _by_node(result)
    assert nodes["node-a1"]["rejectionReason"] == "PodAntiAffinityConflict"
    assert nodes["node-a2"]["rejectionReason"] == "PodAntiAffinityConflict"
    assert nodes["node-a1"]["evidence"]["conflictingPod"] == "web-0"
    assert nodes["node-b1"]["feasible"] is True
    assert result["selectedNode"] == "node-b1"


def test_fixture_zone_skew():
    fixture, result = _run("zone_skew.json")
    nodes = _by_node(result)
    assert nodes["node-a1"]["rejectionReason"] == "TopologySpreadSkew"
    assert nodes["node-a2"]["rejectionReason"] == "TopologySpreadSkew"
    assert nodes["node-a1"]["evidence"]["actualSkew"] == 3
    assert nodes["node-b1"]["feasible"] is True
    assert result["selectedNode"] == "node-b1"


def test_fixture_zone_skew_all_rejected():
    fixture, result = _run("zone_skew_all_rejected.json")
    for node_name, reason in fixture["expectations"]["rejected"].items():
        assert _by_node(result)[node_name]["rejectionReason"] == reason
    assert result["selectedNode"] is None
    assert result["ranking"] == []


def test_fixture_toleration_match():
    fixture, result = _run("toleration_match.json")
    nodes = _by_node(result)
    assert nodes["node-a"]["rejectionReason"] == "TaintNotTolerated"
    assert nodes["node-a"]["evidence"]["taint"]["key"] == "dedicated"
    assert nodes["node-b"]["feasible"] is True
    assert nodes["node-c"]["feasible"] is True
    components = {c["scorer"]: c for c in nodes["node-c"]["score"]["components"]}
    assert components["taintPreference"]["score"] == 90
    # node-b and node-c tie on resources; node-b sorts before node-c by name.
    assert result["selectedNode"] == "node-b"


def test_fixture_zone_spread_soft():
    fixture, result = _run("zone_spread_soft.json")
    nodes = _by_node(result)
    assert nodes["node-a"]["feasible"] is True
    assert nodes["node-b"]["feasible"] is True
    spread_a = next(c for c in nodes["node-a"]["score"]["components"] if c["scorer"] == "topologySpread")
    spread_b = next(c for c in nodes["node-b"]["score"]["components"] if c["scorer"] == "topologySpread")
    assert spread_a["score"] == 0
    assert spread_b["score"] == 50
    assert result["selectedNode"] == "node-b"
