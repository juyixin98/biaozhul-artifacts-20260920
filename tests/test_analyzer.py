"""Core reachability semantics: default deny, both-sides gating, policy union,
named ports, namespace label changes, ipBlock exclusions, and the
empty-selector vs missing-field distinctions."""

from __future__ import annotations


import pytest

from app.analyzer import UnsupportedProtocolError, ValidationError
from app.models import QueryRequest
from tests.conftest import analyzer, pod, policy, q, q_ip, sel, ns


# --------------------------------------------------------------------------- #
# Default-deny
# --------------------------------------------------------------------------- #


def test_no_policy_allows_everything():
    a = analyzer(pods=(pod("c", ips=["10.0.0.1"]), pod("s", ips=["10.0.0.2"])))
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r["reachable"] is True
    assert r["ingress"]["isolated"] is False
    assert r["egress"]["isolated"] is False


def test_default_deny_ingress_empty_rule_list_blocks_all():
    # policyTypes includes Ingress but no ingress rules => deny all inbound
    p = policy({
        "name": "deny",
        "podSelector": {},
        "policyTypes": ["Ingress"],
        "ingress": [],
    })
    a = analyzer(pods=(pod("c", ips=["10.0.0.1"]), pod("s", ips=["10.0.0.2"])),
                 policies=(p,))
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r["reachable"] is False
    assert r["ingress"]["isolated"] is True
    assert r["ingress"]["allowed"] is False
    assert r["ingress"]["allowedBy"] == []


def test_default_deny_both_directions_when_policy_types_include_egress():
    p = policy({
        "name": "deny-both",
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
    })
    a = analyzer(pods=(pod("c"), pod("s")), policies=(p,))
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r["reachable"] is False
    assert r["ingress"]["allowed"] is False
    assert r["egress"]["allowed"] is False


def test_one_sided_deny_blocks_even_when_other_side_allows():
    # Client fully allowed egress; server denies ingress.
    deny = policy({
        "name": "server-deny",
        "podSelector": {"matchLabels": {"app": "server"}},
        "policyTypes": ["Ingress"],
        "ingress": [],
    })
    a = analyzer(
        pods=(
            pod("c", labels={"app": "client"}, ips=["10.0.0.1"]),
            pod("s", labels={"app": "server"}, ips=["10.0.0.2"]),
        ),
        policies=(deny,),
    )
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r["egress"]["allowed"] is True and r["egress"]["isolated"] is False
    assert r["ingress"]["allowed"] is False
    assert r["reachable"] is False


# --------------------------------------------------------------------------- #
# Both sides must allow
# --------------------------------------------------------------------------- #


def test_egress_allowed_but_ingress_denied_is_unreachable():
    egress = policy({
        "name": "c-egress",
        "podSelector": {"matchLabels": {"role": "client"}},
        "policyTypes": ["Egress"],
        "egress": [{"to": [{"podSelector": {}}]}],
    })
    deny = policy({
        "name": "s-deny",
        "podSelector": {"matchLabels": {"role": "server"}},
        "policyTypes": ["Ingress"],
        "ingress": [],
    })
    a = analyzer(
        pods=(pod("c", labels={"role": "client"}), pod("s", labels={"role": "server"})),
        policies=(egress, deny),
    )
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r["egress"]["allowed"] is True
    assert r["ingress"]["allowed"] is False
    assert r["reachable"] is False


# --------------------------------------------------------------------------- #
# Union across policies and rules
# --------------------------------------------------------------------------- #


def test_multiple_policies_allow_sets_are_union():
    # Two ingress policies; each allows a different source, both must permit.
    p1 = policy({
        "name": "allow-a",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "a"}}}],
                     "ports": [{"port": 80, "protocol": "TCP"}]}],
    })
    p2 = policy({
        "name": "allow-b",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "b"}}}],
                     "ports": [{"port": 80, "protocol": "TCP"}]}],
    })
    a = analyzer(
        pods=(pod("a", labels={"app": "a"}), pod("b", labels={"app": "b"}),
              pod("s", labels={"app": "s"})),
        policies=(p1, p2),
    )
    assert a.analyze(QueryRequest.model_validate(q("a", "s", port=80)))["reachable"]
    assert a.analyze(QueryRequest.model_validate(q("b", "s", port=80)))["reachable"]
    r = a.analyze(QueryRequest.model_validate(q("s", "a", port=80)))
    # 'a' is not isolated for ingress, so true; isolate it explicitly:
    assert r["reachable"] is True
    # A third, unmatched source must still be denied:
    a2 = analyzer(
        pods=(pod("a", labels={"app": "a"}), pod("b", labels={"app": "b"}),
              pod("s", labels={"app": "s"}), pod("x", labels={"app": "x"})),
        policies=(p1, p2),
    )
    rx = a2.analyze(QueryRequest.model_validate(q("x", "s", port=80)))
    assert rx["reachable"] is False
    assert len(rx["ingress"]["selectingPolicies"]) == 2


def test_evidence_names_policies_rules_and_ports():
    p = policy({
        "name": "ev",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"podSelector": {"matchLabels": {"app": "c"}}}],
            "ports": [{"port": 80, "protocol": "TCP"},
                      {"port": 443, "protocol": "TCP"}],
        }],
    })
    a = analyzer(
        pods=(pod("c", labels={"app": "c"}), pod("s", labels={"app": "s"})),
        policies=(p,),
    )
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=443)))
    assert r["reachable"] is True
    ev = r["ingress"]["allowedBy"]
    assert len(ev) == 1
    assert ev[0]["policy"]["name"] == "ev"
    assert ev[0]["ruleIndex"] == 0
    assert ev[0]["peerIndex"] == 0
    assert ev[0]["allowedPorts"] == "443"
    assert ev[0]["peer"] == "selectors"


# --------------------------------------------------------------------------- #
# Named ports and port ranges
# --------------------------------------------------------------------------- #


def test_named_port_resolves_against_destination_container_ports():
    p = policy({
        "name": "np",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"podSelector": {"matchLabels": {"app": "c"}}}],
            "ports": [{"port": "http", "protocol": "TCP"}],
        }],
    })
    a = analyzer(
        pods=(
            pod("c", labels={"app": "c"},
                ports=[{"name": "http", "containerPort": 9999, "protocol": "TCP"}]),
            pod("s", labels={"app": "s"},
                ports=[{"name": "http", "containerPort": 8080, "protocol": "TCP"}]),
        ),
        policies=(p,),
    )
    # Named port resolves to the SERVER container port 8080, not client's 9999.
    assert a.analyze(QueryRequest.model_validate(q("c", "s", port=8080)))["reachable"]
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=9999)))
    assert r["reachable"] is False


def test_named_port_missing_on_target_grants_nothing():
    p = policy({
        "name": "np",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"podSelector": {}}],
            "ports": [{"port": "ghost", "protocol": "TCP"}],
        }],
    })
    a = analyzer(
        pods=(pod("c"), pod("s", labels={"app": "s"})), policies=(p,))
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r["reachable"] is False


def test_named_port_is_protocol_scoped():
    p = policy({
        "name": "np",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"podSelector": {}}],
            "ports": [{"port": "dns", "protocol": "UDP"}],
        }],
    })
    a = analyzer(
        pods=(
            pod("c"),
            pod("s", labels={"app": "s"},
                ports=[{"name": "dns", "containerPort": 5353, "protocol": "TCP"}]),
        ),
        policies=(p,),
    )
    # Only a TCP container port named dns exists; the UDP rule must not match.
    r = a.analyze(QueryRequest.model_validate(q("c", "s", protocol="UDP", port=5353)))
    assert r["reachable"] is False


def test_port_range_endport_inclusive():
    p = policy({
        "name": "rng",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"podSelector": {}}],
            "ports": [{"port": 9000, "endPort": 9002, "protocol": "TCP"}],
        }],
    })
    a = analyzer(pods=(pod("c"), pod("s", labels={"app": "s"})), policies=(p,))
    for port in (9000, 9001, 9002):
        assert a.analyze(QueryRequest.model_validate(q("c", "s", port=port)))["reachable"]
    assert not a.analyze(QueryRequest.model_validate(q("c", "s", port=8999)))["reachable"]
    assert not a.analyze(QueryRequest.model_validate(q("c", "s", port=9003)))["reachable"]


def test_missing_ports_means_all_ports():
    p = policy({
        "name": "allports",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"podSelector": {}}]}],  # no ports key
    })
    a = analyzer(pods=(pod("c"), pod("s", labels={"app": "s"})), policies=(p,))
    assert a.analyze(QueryRequest.model_validate(q("c", "s", port=1)))["reachable"]
    assert a.analyze(QueryRequest.model_validate(q("c", "s", port=65535)))["reachable"]


def test_port_entry_with_omitted_port_matches_all_ports_of_that_protocol_only():
    # {"protocol":"TCP"} inside ports[] = all TCP ports; UDP is unaffected.
    p = policy({
        "name": "all-tcp",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"podSelector": {}}],
            "ports": [{"protocol": "TCP"}],
        }],
    })
    a = analyzer(pods=(pod("c"), pod("s", labels={"app": "s"})), policies=(p,))
    assert a.analyze(QueryRequest.model_validate(
        q("c", "s", protocol="TCP", port=22)))["reachable"]
    assert a.analyze(QueryRequest.model_validate(
        q("c", "s", protocol="TCP", port=65000)))["reachable"]
    assert not a.analyze(QueryRequest.model_validate(
        q("c", "s", protocol="UDP", port=22)))["reachable"]


# --------------------------------------------------------------------------- #
# Namespace selectors and namespace label changes
# --------------------------------------------------------------------------- #


def _cross_ns_snapshot(ns_labels):
    namespaces = [
        ns("src", ns_labels["src"]),
        ns("dst", ns_labels["dst"]),
    ]
    pods = (
        pod("c", namespace="src", labels={"app": "c"}, ips=["10.1.0.1"]),
        pod("s", namespace="dst", labels={"app": "s"}, ips=["10.2.0.1"],
            ports=[{"name": "http", "containerPort": 8080, "protocol": "TCP"}]),
    )
    egress = policy({
        "name": "c-egress",
        "namespace": "src",
        "podSelector": {"matchLabels": {"app": "c"}},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{
                "namespaceSelector": {"matchLabels": {"trust": "true"}},
                "podSelector": {"matchLabels": {"app": "s"}},
            }],
            "ports": [{"port": "http", "protocol": "TCP"}],
        }],
    })
    ingress = policy({
        "name": "s-ingress",
        "namespace": "dst",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{
                "namespaceSelector": {"matchLabels": {"trust": "true"}},
                "podSelector": {"matchLabels": {"app": "c"}},
            }],
            "ports": [{"port": "http", "protocol": "TCP"}],
        }],
    })
    return analyzer(namespaces=namespaces, pods=pods, policies=(egress, ingress))


def test_cross_namespace_allowed_when_labels_match():
    a = _cross_ns_snapshot({"src": {"trust": "true"}, "dst": {"trust": "true"}})
    r = a.analyze(QueryRequest.model_validate(
        q("c", "s", port=8080, src_ns="src", dst_ns="dst")))
    assert r["reachable"] is True


def test_namespace_label_change_revokes_reachability():
    # Initially both namespaces trusted; then the destination loses the label.
    a_before = _cross_ns_snapshot(
        {"src": {"trust": "true"}, "dst": {"trust": "true"}})
    assert a_before.analyze(QueryRequest.model_validate(
        q("c", "s", port=8080, src_ns="src", dst_ns="dst")))["reachable"] is True

    a_after = _cross_ns_snapshot(
        {"src": {"trust": "true"}, "dst": {"trust": "false"}})
    r = a_after.analyze(QueryRequest.model_validate(
        q("c", "s", port=8080, src_ns="src", dst_ns="dst")))
    assert r["reachable"] is False
    # The egress side is what now rejects (server ns no longer trusted).
    assert r["egress"]["allowed"] is False
    assert r["ingress"]["allowed"] is True


def test_namespace_selector_alone_selects_all_pods_of_matching_namespaces():
    namespaces = [ns("a", {"team": "one"}), ns("b", {"team": "two"})]
    pods = (
        pod("c", namespace="a", labels={"app": "c"}),
        pod("s1", namespace="b", labels={"app": "anything"}),
        pod("s2", namespace="b", labels={"other": "label"}),
    )
    p = policy({
        "name": "ns-only",
        "namespace": "a",
        "podSelector": {"matchLabels": {"app": "c"}},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{"namespaceSelector": {"matchLabels": {"team": "two"}}}],
        }],
    })
    a = analyzer(namespaces=namespaces, pods=pods, policies=(p,))
    assert a.analyze(QueryRequest.model_validate(
        q("c", "s1", port=1234, src_ns="a", dst_ns="b")))["reachable"]
    assert a.analyze(QueryRequest.model_validate(
        q("c", "s2", port=1234, src_ns="a", dst_ns="b")))["reachable"]
    # namespaceSelector present overrides the "policy namespace" default, so
    # pods in namespace 'a' (whose labels do not match team=two) are not peers.
    r = a.analyze(QueryRequest.model_validate(
        q("c", "c", port=1234, src_ns="a", dst_ns="a")))
    assert r["egress"]["allowed"] is False


def test_pod_selector_without_namespace_selector_is_policy_namespace_only():
    namespaces = [ns("one"), ns("two")]
    pods = (
        pod("c", namespace="one", labels={"app": "c"}),
        pod("same", namespace="one", labels={"app": "s"}),
        pod("other", namespace="two", labels={"app": "s"}),
    )
    p = policy({
        "name": "local",
        "namespace": "one",
        "podSelector": {"matchLabels": {"app": "c"}},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{"podSelector": {"matchLabels": {"app": "s"}}}],
        }],
    })
    a = analyzer(namespaces=namespaces, pods=pods, policies=(p,))
    assert a.analyze(QueryRequest.model_validate(
        q("c", "same", src_ns="one", dst_ns="one")))["reachable"]
    r = a.analyze(QueryRequest.model_validate(
        q("c", "other", src_ns="one", dst_ns="two")))
    assert r["egress"]["allowed"] is False


# --------------------------------------------------------------------------- #
# ipBlock + except
# --------------------------------------------------------------------------- #


def test_ipblock_allows_within_cidr_and_except_excludes():
    p = policy({
        "name": "ib",
        "podSelector": {"matchLabels": {"app": "c"}},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{"ipBlock": {
                "cidr": "10.5.0.0/24",
                "except": ["10.5.0.128/25"],
            }}],
        }],
    })
    a = analyzer(pods=(pod("c", labels={"app": "c"}),), policies=(p,))
    assert a.analyze(QueryRequest.model_validate(q_ip("c", "10.5.0.1")))["reachable"]
    assert a.analyze(QueryRequest.model_validate(q_ip("c", "10.5.0.127")))["reachable"]
    r = a.analyze(QueryRequest.model_validate(q_ip("c", "10.5.0.128")))
    assert r["reachable"] is False
    assert r["egress"]["allowedBy"] == []
    r2 = a.analyze(QueryRequest.model_validate(q_ip("c", "10.5.0.255")))
    assert r2["reachable"] is False
    r3 = a.analyze(QueryRequest.model_validate(q_ip("c", "10.6.0.1")))
    assert r3["reachable"] is False


def test_ipblock_matches_pod_endpoint_by_its_ip():
    p = policy({
        "name": "ib",
        "podSelector": {},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"ipBlock": {"cidr": "10.9.0.0/16"}}],
        }],
    })
    a = analyzer(
        pods=(
            pod("c", labels={"app": "c"}, ips=["10.9.1.1"]),
            pod("s", labels={"app": "s"}, ips=["10.0.0.2"]),
        ),
        policies=(p,),
    )
    assert a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))["reachable"]
    p2 = policy({
        "name": "ib2",
        "podSelector": {},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"ipBlock": {"cidr": "10.9.0.0/16", "except": ["10.9.1.1/32"]}}],
        }],
    })
    a2 = analyzer(
        pods=(
            pod("c", labels={"app": "c"}, ips=["10.9.1.1"]),
            pod("s", labels={"app": "s"}, ips=["10.0.0.2"]),
        ),
        policies=(p2,),
    )
    assert a2.analyze(QueryRequest.model_validate(q("c", "s", port=80)))["reachable"] is False


def test_ipv6_ipblock_supported():
    p = policy({
        "name": "v6",
        "podSelector": {},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{"ipBlock": {"cidr": "fd00::/80", "except": ["fd00::1/128"]}}],
        }],
    })
    a = analyzer(pods=(pod("c"),), policies=(p,))
    assert a.analyze(QueryRequest.model_validate(q_ip("c", "fd00::2")))["reachable"]
    assert not a.analyze(QueryRequest.model_validate(q_ip("c", "fd00::1")))["reachable"]
    assert not a.analyze(QueryRequest.model_validate(q_ip("c", "fe00::2")))["reachable"]


def test_invalid_ipblock_inputs_rejected():
    from app.models import IPBlock
    with pytest.raises(Exception):
        IPBlock.model_validate({"cidr": "not-a-cidr"})
    with pytest.raises(Exception):
        IPBlock.model_validate({"cidr": "10.0.0.0/24", "except": ["10.0.1.0/24"]})
    with pytest.raises(Exception):
        IPBlock.model_validate({"cidr": "10.0.0.0/24",
                                "except": ["10.0.0.0/23"]})  # not contained
    with pytest.raises(Exception):
        IPBlock.model_validate({"cidr": "10.0.0.0/24",
                                "except": ["10.0.0.0/25", "10.0.0.128/25",
                                           "10.0.0.64/26"]})  # overlap
    # except equal to the whole cidr is permitted (excludes everything)
    block = IPBlock.model_validate({"cidr": "10.0.0.0/24",
                                    "except": ["10.0.0.0/24"]})
    assert block.except_ == ("10.0.0.0/24",)


# --------------------------------------------------------------------------- #
# Empty selector vs missing field distinctions
# --------------------------------------------------------------------------- #


def test_explicit_empty_policytypes_isolates_nothing():
    p = policy({
        "name": "no-op",
        "podSelector": {},
        "policyTypes": [],
        "ingress": [],
        "egress": [],
    })
    a = analyzer(pods=(pod("c"), pod("s")), policies=(p,))
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r["ingress"]["isolated"] is False
    assert r["egress"]["isolated"] is False
    assert r["reachable"] is True


def test_missing_policy_types_defaults_ingress_always_egress_if_rules_exist():
    # Kubernetes defaulting when policyTypes is absent:
    #   Ingress is ALWAYS included; Egress is included only if >=1 egress
    #   rule exists. Explicit policyTypes: [] changes this (tested elsewhere),
    #   which is the empty-vs-missing distinction.
    p_ing = policy({
        "name": "implicit-ingress-empty",
        "podSelector": {},
        "ingress": [],  # zero rules, but Ingress still defaulted on => deny
    })
    a = analyzer(pods=(pod("c"), pod("s")), policies=(p_ing,))
    r = a.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r["ingress"]["isolated"] is True
    assert r["ingress"]["allowed"] is False
    assert r["egress"]["isolated"] is False
    assert r["reachable"] is False

    p_eg = policy({
        "name": "implicit-egress-empty",
        "podSelector": {},
        "egress": [],  # zero rules -> Egress NOT auto-added
    })
    a2 = analyzer(pods=(pod("c"), pod("s")), policies=(p_eg,))
    r2 = a2.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r2["egress"]["isolated"] is False
    assert r2["ingress"]["isolated"] is True  # Ingress always defaulted on

    # A real egress rule flips Egress on as well.
    p_eg2 = policy({
        "name": "implicit-egress-present",
        "podSelector": {},
        "egress": [{"to": [{"podSelector": {}}]}],
    })
    a3 = analyzer(pods=(pod("c"), pod("s")), policies=(p_eg2,))
    r3 = a3.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r3["egress"]["isolated"] is True
    assert r3["ingress"]["isolated"] is True


def test_empty_peer_matches_policy_namespace_but_not_external_ips():
    p = policy({
        "name": "local-peer",
        "podSelector": {},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": [{}]}],
    })
    namespaces = [ns("default"), ns("other")]
    pods = (
        pod("c", namespace="default", ips=["10.0.0.1"]),
        pod("oc", namespace="other", ips=["10.0.0.9"]),
        pod("s", namespace="default", ips=["10.0.0.2"]),
    )
    a = analyzer(namespaces=namespaces, pods=pods, policies=(p,))
    assert a.analyze(QueryRequest.model_validate(q("c", "s")))["reachable"]
    r = a.analyze(QueryRequest.model_validate(q("oc", "s", src_ns="other")))
    assert r["reachable"] is False


def test_missing_from_list_matches_external_ips_but_empty_from_list_matches_nobody():
    p_missing = policy({
        "name": "missing-from",
        "podSelector": {},
        "policyTypes": ["Ingress"],
        "ingress": [{}],  # no 'from' key at all
    })
    a1 = analyzer(pods=(pod("s"),), policies=(p_missing,))
    r1 = a1.analyze(QueryRequest.model_validate({
        "source": {"ip": "8.8.8.8"},
        "destination": {"pod": {"namespace": "default", "name": "s"}},
        "port": 80,
    }))
    assert r1["reachable"] is True
    assert r1["ingress"]["allowedBy"][0]["peer"] == "wildcard(all-endpoints)"

    p_empty = policy({
        "name": "empty-from",
        "podSelector": {},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": []}],
    })
    a2 = analyzer(pods=(pod("c"), pod("s")), policies=(p_empty,))
    r2 = a2.analyze(QueryRequest.model_validate(q("c", "s", port=80)))
    assert r2["reachable"] is False
    assert r2["ingress"]["allowedBy"] == []


def test_empty_namespace_selector_matches_all_namespaces():
    namespaces = [ns("a", {"env": "dev"}), ns("b", {"env": "prod"})]
    pods = (
        pod("c", namespace="a"),
        pod("s", namespace="b", labels={"app": "s"}),
    )
    p = policy({
        "name": "all-ns",
        "namespace": "a",
        "podSelector": {},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{
                "namespaceSelector": {},
                "podSelector": {"matchLabels": {"app": "s"}},
            }],
        }],
    })
    a = analyzer(namespaces=namespaces, pods=pods, policies=(p,))
    assert a.analyze(QueryRequest.model_validate(
        q("c", "s", src_ns="a", dst_ns="b")))["reachable"]


# --------------------------------------------------------------------------- #
# Unknown / unsupported protocols
# --------------------------------------------------------------------------- #


def test_unknown_protocol_query_rejected_not_guessed():
    a = analyzer(pods=(pod("c"), pod("s")))
    with pytest.raises(UnsupportedProtocolError):
        a.analyze(QueryRequest.model_validate(
            q("c", "s", protocol="SCTP", port=80)))
    with pytest.raises(UnsupportedProtocolError):
        a.analyze(QueryRequest.model_validate(
            q("c", "s", protocol="GRE", port=80)))


def test_sctp_policy_does_not_grant_tcp_or_udp_and_warns():
    p = policy({
        "name": "sctp",
        "podSelector": {},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{}],
            "ports": [{"port": 5000, "protocol": "SCTP"}],
        }],
    })
    a = analyzer(pods=(pod("c"), pod("s")), policies=(p,))
    assert any("SCTP" in w for w in a.warnings)
    for proto in ("TCP", "UDP"):
        r = a.analyze(QueryRequest.model_validate(
            q("c", "s", protocol=proto, port=5000)))
        assert r["reachable"] is False


def test_udp_is_distinct_from_tcp():
    p = policy({
        "name": "udp-only",
        "podSelector": {},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{}],
            "ports": [{"port": 53, "protocol": "UDP"}],
        }],
    })
    a = analyzer(pods=(pod("c"), pod("s")), policies=(p,))
    assert a.analyze(QueryRequest.model_validate(
        q("c", "s", protocol="UDP", port=53)))["reachable"]
    assert not a.analyze(QueryRequest.model_validate(
        q("c", "s", protocol="TCP", port=53)))["reachable"]


# --------------------------------------------------------------------------- #
# Immutable snapshot
# --------------------------------------------------------------------------- #


def test_snapshot_models_are_frozen():
    p = pod("c")
    with pytest.raises(Exception):
        p.name = "hacked"  # type: ignore[misc]
    a = analyzer(pods=(pod("c"), pod("s")))
    with pytest.raises(Exception):
        a.snapshot.pods = ("x",)  # type: ignore[misc]


def test_analyzer_does_not_match_by_name_substring():
    # Selector {app: "s"} must not match pod named "sservice" via name strings.
    p = policy({
        "name": "substr",
        "podSelector": {"matchLabels": {"app": "s"}},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "c"}}}]}],
    })
    a = analyzer(
        pods=(
            pod("client-something", labels={"app": "c"}),
            pod("sservice", labels={"app": "totally-different"}),
        ),
        policies=(p,),
    )
    r = a.analyze(QueryRequest.model_validate(q("client-something", "sservice")))
    # target pod not selected by the policy => non-isolated => allowed; but the
    # selectors themselves must never claim a match. Direct selector checks:
    from app.selectors import selector_matches as sm
    assert not sm(sel({"app": "s"}), {"app": "totally-different"})
    assert not sm(sel({"app": "c"}), {"application": "c"})
    assert r["ingress"]["isolated"] is False


def test_unknown_endpoint_pod_raises_validation_error():
    a = analyzer(pods=(pod("c"),))
    with pytest.raises(ValidationError):
        a.analyze(QueryRequest.model_validate(q("c", "ghost")))
