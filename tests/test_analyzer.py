"""Unit tests for the core analyzer.

Selectors are exercised structurally (label maps in, boolean reachability
out); no test relies on string matching of selector text.
"""

from __future__ import annotations

import pytest

from app.analyzer import AnalysisError, analyze, selector_matches
from app.models import (
    LabelSelector,
    Pod,
    ReachabilityQuery,
    Snapshot,
)


def make_pod(name, namespace, labels=None, ip=None, ports=()):
    return Pod(
        name=name,
        namespace=namespace,
        labels=labels or {},
        ip=ip,
        ports=ports,
    )


def query(src_ns, src, dst_ns, dst, port, protocol="TCP"):
    return ReachabilityQuery(
        source={"namespace": src_ns, "name": src},
        destination={"namespace": dst_ns, "name": dst},
        protocol=protocol,
        port=port,
    )


def policy(name, namespace, spec):
    return {"metadata": {"name": name, "namespace": namespace}, "spec": spec}


# ---------------------------------------------------------------------------
# Default deny
# ---------------------------------------------------------------------------

def test_empty_ingress_list_is_default_deny():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}, {"name": "b", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod("dst", "b", labels={"app": "x"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("deny-all", "b", {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [],
            })
        ],
    )
    result = analyze(snap, query("a", "src", "b", "dst", 80))
    assert result["reachable"] is False
    assert result["ingress"]["isolated"] is True
    assert result["ingress"]["allowed"] is False
    assert result["egress"]["isolated"] is False
    assert result["egress"]["allowed"] is True


def test_missing_ingress_field_does_not_isolate():
    """A policy that only governs egress must not isolate ingress."""
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[
            make_pod("src", "a", labels={"app": "s"}, ip="10.0.0.1"),
            make_pod("dst", "a", labels={"app": "d"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("egress-only", "a", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Egress"],
                "egress": [],
            })
        ],
    )
    result = analyze(snap, query("a", "src", "a", "dst", 80))
    assert result["ingress"]["isolated"] is False
    assert result["reachable"] is True


def test_implicit_policy_types_defaulting():
    """No policyTypes: ingress always governed, egress only if present."""
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[
            make_pod("p1", "a", labels={"app": "x"}, ip="10.0.0.1"),
            make_pod("p2", "a", labels={"app": "x"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("implicit", "a", {
                "podSelector": {"matchLabels": {"app": "x"}},
                "ingress": [],
            })
        ],
    )
    result = analyze(snap, query("a", "p1", "a", "p2", 80))
    assert result["ingress"]["isolated"] is True
    assert result["egress"]["isolated"] is False
    assert result["reachable"] is False


# ---------------------------------------------------------------------------
# Empty selector {} vs missing field
# ---------------------------------------------------------------------------

def test_empty_pod_selector_matches_all_pods_in_namespace():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod("dst", "a", labels={"whatever": "yes"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("deny-all", "a", {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [],
            })
        ],
    )
    result = analyze(snap, query("a", "src", "a", "dst", 80))
    assert result["ingress"]["isolated"] is True
    assert result["reachable"] is False


def test_empty_namespace_selector_matches_all_namespaces():
    snap = Snapshot(
        namespaces=[
            {"name": "a", "labels": {"team": "one"}},
            {"name": "b", "labels": {"team": "two"}},
        ],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod("dst", "b", labels={"app": "d"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("allow-all-ns", "b", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"namespaceSelector": {}}]}],
            })
        ],
    )
    result = analyze(snap, query("a", "src", "b", "dst", 80))
    assert result["reachable"] is True
    assert result["ingress"]["evidence"][0]["policy"] == "b/allow-all-ns"


def test_pod_selector_without_namespace_selector_is_policy_namespace_only():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}, {"name": "b", "labels": {}}],
        pods=[
            # Same labels, different namespaces.
            make_pod("src", "a", labels={"app": "web"}, ip="10.0.0.1"),
            make_pod("dst", "b", labels={"app": "d"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("allow-web-same-ns", "b", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "web"}}}]}],
            })
        ],
    )
    # src lives in namespace "a", the policy in "b": podSelector-only peers
    # never cross namespaces.
    result = analyze(snap, query("a", "src", "b", "dst", 80))
    assert result["reachable"] is False
    assert result["ingress"]["allowed"] is False


# ---------------------------------------------------------------------------
# Named ports
# ---------------------------------------------------------------------------

def test_named_port_resolves_against_destination_container_ports():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}, {"name": "b", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod(
                "dst", "b", labels={"app": "d"}, ip="10.0.0.2",
                ports=[{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
            ),
        ],
        policies=[
            policy("allow-http", "b", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"protocol": "TCP", "port": "http"}]}],
            })
        ],
    )
    # Querying the resolved number works...
    assert analyze(snap, query("a", "src", "b", "dst", 8080))["reachable"] is True
    # ...querying by the same name works...
    assert analyze(snap, query("a", "src", "b", "dst", "http"))["reachable"] is True
    # ...and a different port is denied.
    denied = analyze(snap, query("a", "src", "b", "dst", 9090))
    assert denied["reachable"] is False
    assert denied["ingress"]["allowed"] is False


def test_unresolved_named_port_is_reported():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod("dst", "a", ip="10.0.0.2"),
        ],
    )
    result = analyze(snap, query("a", "src", "a", "dst", "no-such-port"))
    assert result["status"] == "unresolved_named_port"
    assert result["reachable"] is False


def test_named_port_protocol_must_match():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod(
                "dst", "a", labels={"app": "d"}, ip="10.0.0.2",
                ports=[{"name": "dns", "containerPort": 53, "protocol": "UDP"}],
            ),
        ],
        policies=[
            policy("allow-dns-udp", "a", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"protocol": "UDP", "port": "dns"}]}],
            })
        ],
    )
    assert analyze(snap, query("a", "src", "a", "dst", 53, protocol="UDP"))["reachable"] is True
    # Same numeric port over TCP is not covered by the UDP entry.
    assert analyze(snap, query("a", "src", "a", "dst", 53, protocol="TCP"))["reachable"] is False


# ---------------------------------------------------------------------------
# Namespace label changes
# ---------------------------------------------------------------------------

def _two_ns_snapshot(team_label):
    return Snapshot(
        namespaces=[
            {"name": "clients", "labels": {"team": team_label}},
            {"name": "servers", "labels": {"team": "api"}},
        ],
        pods=[
            make_pod("client", "clients", ip="10.0.0.1"),
            make_pod("server", "servers", labels={"app": "api"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("allow-frontend-ns", "servers", {
                "podSelector": {"matchLabels": {"app": "api"}},
                "policyTypes": ["Ingress"],
                "ingress": [{
                    "from": [{"namespaceSelector": {"matchLabels": {"team": "frontend"}}}],
                }],
            })
        ],
    )


def test_namespace_label_change_flips_reachability():
    allowed = analyze(
        _two_ns_snapshot("frontend"),
        query("clients", "client", "servers", "server", 443),
    )
    denied = analyze(
        _two_ns_snapshot("backend"),
        query("clients", "client", "servers", "server", 443),
    )
    assert allowed["reachable"] is True
    assert denied["reachable"] is False
    assert denied["ingress"]["isolated"] is True
    assert denied["ingress"]["evidence"] == []


# ---------------------------------------------------------------------------
# ipBlock and except
# ---------------------------------------------------------------------------

def _ipblock_snapshot():
    return Snapshot(
        namespaces=[{"name": "a", "labels": {}}, {"name": "ext", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod("in-range", "ext", ip="192.168.1.20"),
            make_pod("excluded", "ext", ip="192.168.1.10"),
            make_pod("out-of-range", "ext", ip="203.0.113.5"),
        ],
        policies=[
            policy("egress-cidr", "a", {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [{
                    "to": [{"ipBlock": {
                        "cidr": "192.168.1.0/24",
                        "except": ["192.168.1.10/32"],
                    }}],
                }],
            })
        ],
    )


def test_ipblock_cidr_allows_and_except_denies():
    snap = _ipblock_snapshot()
    assert analyze(snap, query("a", "src", "ext", "in-range", 443))["reachable"] is True
    excluded = analyze(snap, query("a", "src", "ext", "excluded", 443))
    assert excluded["reachable"] is False
    assert excluded["egress"]["allowed"] is False
    outside = analyze(snap, query("a", "src", "ext", "out-of-range", 443))
    assert outside["reachable"] is False
    assert outside["egress"]["allowed"] is False


# ---------------------------------------------------------------------------
# Union semantics across policies
# ---------------------------------------------------------------------------

def test_allow_sets_union_across_policies():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}, {"name": "b", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod("dst", "b", labels={"app": "d"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("allow-80", "b", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"protocol": "TCP", "port": 80}]}],
            }),
            policy("allow-443", "b", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"protocol": "TCP", "port": 443}]}],
            }),
        ],
    )
    assert analyze(snap, query("a", "src", "b", "dst", 80))["reachable"] is True
    assert analyze(snap, query("a", "src", "b", "dst", 443))["reachable"] is True
    assert analyze(snap, query("a", "src", "b", "dst", 22))["reachable"] is False


def test_both_sides_must_allow():
    """Egress allows, ingress denies -> unreachable, and vice versa."""
    base_pods = [
        make_pod("src", "a", labels={"app": "s"}, ip="10.0.0.1"),
        make_pod("dst", "b", labels={"app": "d"}, ip="10.0.0.2"),
    ]
    nss = [{"name": "a", "labels": {}}, {"name": "b", "labels": {}}]
    egress_open_ingress_closed = Snapshot(
        namespaces=nss,
        pods=base_pods,
        policies=[
            policy("deny-ingress", "b", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [],
            })
        ],
    )
    result = analyze(egress_open_ingress_closed, query("a", "src", "b", "dst", 80))
    assert result["egress"]["allowed"] is True
    assert result["ingress"]["allowed"] is False
    assert result["reachable"] is False

    ingress_open_egress_closed = Snapshot(
        namespaces=nss,
        pods=base_pods,
        policies=[
            policy("deny-egress", "a", {
                "podSelector": {"matchLabels": {"app": "s"}},
                "policyTypes": ["Egress"],
                "egress": [],
            })
        ],
    )
    result = analyze(ingress_open_egress_closed, query("a", "src", "b", "dst", 80))
    assert result["egress"]["allowed"] is False
    assert result["ingress"]["allowed"] is True
    assert result["reachable"] is False


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

def test_unsupported_protocol_is_reported_not_analyzed():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[make_pod("src", "a"), make_pod("dst", "a")],
    )
    result = analyze(snap, query("a", "src", "a", "dst", 443, protocol="SCTP"))
    assert result["status"] == "unsupported_protocol"
    assert result["reachable"] is False
    assert "SCTP" in result["detail"]


def test_udp_rules_do_not_leak_into_tcp():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod("dst", "a", labels={"app": "d"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("udp-only", "a", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"protocol": "UDP", "port": 53}]}],
            })
        ],
    )
    assert analyze(snap, query("a", "src", "a", "dst", 53, protocol="UDP"))["reachable"] is True
    assert analyze(snap, query("a", "src", "a", "dst", 53, protocol="TCP"))["reachable"] is False


# ---------------------------------------------------------------------------
# Selector operators and port ranges
# ---------------------------------------------------------------------------

def test_match_expressions_operators():
    sel = LabelSelector.model_validate({
        "matchExpressions": [
            {"key": "env", "operator": "In", "values": ["prod", "staging"]},
            {"key": "deprecated", "operator": "DoesNotExist"},
        ]
    })
    assert selector_matches(sel, {"env": "prod"}) is True
    assert selector_matches(sel, {"env": "dev"}) is False
    assert selector_matches(sel, {"env": "prod", "deprecated": "true"}) is False
    with pytest.raises(AnalysisError):
        selector_matches(
            LabelSelector.model_validate({
                "matchExpressions": [{"key": "x", "operator": "Bogus"}]
            }),
            {},
        )


def test_end_port_range():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[
            make_pod("src", "a", ip="10.0.0.1"),
            make_pod("dst", "a", labels={"app": "d"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("range", "a", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"protocol": "TCP", "port": 8000, "endPort": 9000}]}],
            })
        ],
    )
    assert analyze(snap, query("a", "src", "a", "dst", 8500))["reachable"] is True
    assert analyze(snap, query("a", "src", "a", "dst", 9000))["reachable"] is True
    assert analyze(snap, query("a", "src", "a", "dst", 9001))["reachable"] is False


# ---------------------------------------------------------------------------
# Evidence and immutability
# ---------------------------------------------------------------------------

def test_evidence_names_matching_policy_and_rule():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[
            make_pod("src", "a", labels={"app": "s"}, ip="10.0.0.1"),
            make_pod("dst", "a", labels={"app": "d"}, ip="10.0.0.2"),
        ],
        policies=[
            policy("allow-same-ns", "a", {
                "podSelector": {"matchLabels": {"app": "d"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "s"}}}]}],
            })
        ],
    )
    result = analyze(snap, query("a", "src", "a", "dst", 80))
    assert result["reachable"] is True
    ev = result["ingress"]["evidence"]
    assert len(ev) == 1
    assert ev[0]["policy"] == "a/allow-same-ns"
    assert ev[0]["ruleIndex"] == 0
    assert ev[0]["peerIndex"] == 0


def test_snapshot_is_immutable():
    snap = Snapshot(
        namespaces=[{"name": "a", "labels": {}}],
        pods=[make_pod("src", "a")],
    )
    with pytest.raises(Exception):
        snap.pods[0].name = "mutated"
    with pytest.raises(Exception):
        snap.namespaces = ()


def test_unknown_pod_raises():
    snap = Snapshot(namespaces=[{"name": "a", "labels": {}}], pods=[make_pod("src", "a")])
    with pytest.raises(AnalysisError):
        analyze(snap, query("a", "src", "a", "ghost", 80))
