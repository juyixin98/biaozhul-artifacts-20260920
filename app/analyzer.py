"""Core reachability analysis.

The analyzer evaluates Kubernetes NetworkPolicy semantics offline against an
immutable :class:`~app.models.Snapshot`:

* A pod becomes **isolated** for a direction as soon as at least one policy
  selects it and governs that direction (via ``policyTypes`` or the implicit
  defaulting rules).  Non-isolated pods allow all traffic in that direction.
* For isolated pods the allowed set is the **union** of the rules of every
  policy that selects the pod for that direction.
* Traffic is reachable only when the source's egress **and** the
  destination's ingress both allow it.
* Selectors are evaluated structurally against label maps — never by string
  comparison — and an empty selector (``{}``) matches everything while a
  missing selector field changes the peer's meaning entirely.
"""

from __future__ import annotations

import ipaddress
from typing import Optional, Union

from .models import (
    LabelSelector,
    NetworkPolicy,
    NetworkPolicyPeer,
    NetworkPolicyPort,
    Pod,
    ReachabilityQuery,
    Snapshot,
)

SUPPORTED_PROTOCOLS = ("TCP", "UDP")


class AnalysisError(Exception):
    """Raised for malformed input that prevents analysis."""


# ---------------------------------------------------------------------------
# Selector evaluation (structural, never string matching)
# ---------------------------------------------------------------------------

def selector_matches(selector: LabelSelector, labels: dict[str, str]) -> bool:
    """Return True when ``labels`` satisfies ``selector``.

    An empty selector matches everything.  ``matchLabels`` and every
    ``matchExpressions`` entry must all hold (logical AND).
    """
    for key, value in selector.matchLabels.items():
        if labels.get(key) != value:
            return False
    for expr in selector.matchExpressions:
        op = expr.operator
        present = expr.key in labels
        if op == "In":
            if not (present and labels[expr.key] in expr.values):
                return False
        elif op == "NotIn":
            if present and labels[expr.key] in expr.values:
                return False
        elif op == "Exists":
            if not present:
                return False
        elif op == "DoesNotExist":
            if present:
                return False
        else:
            raise AnalysisError(f"unsupported matchExpression operator: {op!r}")
    return True


# ---------------------------------------------------------------------------
# Peer evaluation
# ---------------------------------------------------------------------------

def _ip_in_block(ip: str, cidr: str, except_: tuple[str, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise AnalysisError(f"invalid ipBlock cidr {cidr!r}: {exc}") from exc
    if addr not in network:
        return False
    for exc_cidr in except_:
        try:
            exc_net = ipaddress.ip_network(exc_cidr, strict=False)
        except ValueError as e:
            raise AnalysisError(f"invalid ipBlock except entry {exc_cidr!r}: {e}") from e
        if addr in exc_net:
            return False
    return True


def peer_matches_pod(
    peer: NetworkPolicyPeer,
    pod: Pod,
    pod_namespace_labels: dict[str, str],
    policy_namespace: str,
) -> bool:
    """Evaluate one peer against a concrete pod.

    Kubernetes semantics:
    * ``podSelector`` + ``namespaceSelector``: pods matching podSelector that
      live in namespaces matching namespaceSelector.
    * ``podSelector`` alone: pods matching it in the **policy's own**
      namespace.
    * ``namespaceSelector`` alone: all pods in matching namespaces.
    * ``ipBlock``: matches on the pod's IP, honouring ``except`` entries.
    """
    if peer.ipBlock is not None:
        if pod.ip is None:
            return False
        return _ip_in_block(pod.ip, peer.ipBlock.cidr, peer.ipBlock.except_)

    if peer.namespaceSelector is not None:
        if not selector_matches(peer.namespaceSelector, pod_namespace_labels):
            return False
        if peer.podSelector is not None:
            return selector_matches(peer.podSelector, pod.labels)
        return True

    if peer.podSelector is not None:
        if pod.namespace != policy_namespace:
            return False
        return selector_matches(peer.podSelector, pod.labels)

    # An empty peer ({}) selects nothing.
    return False


# ---------------------------------------------------------------------------
# Port evaluation
# ---------------------------------------------------------------------------

def _resolve_named_port(pod: Pod, name: str, protocol: str) -> Optional[int]:
    """Resolve a named port against a pod's declared container ports."""
    for cp in pod.ports:
        if cp.name == name and cp.protocol.upper() == protocol:
            return cp.containerPort
    return None


def port_entry_matches(
    entry: NetworkPolicyPort,
    protocol: str,
    port: int,
    named_port_target: Optional[Pod],
) -> bool:
    """Check one policy port entry against concrete traffic.

    ``named_port_target`` is the pod whose container ports resolve named
    ports (the destination pod for both ingress and egress rules).
    """
    if entry.protocol.upper() != protocol:
        return False
    if entry.port is None:
        # No port restriction: the entry covers every port of the protocol.
        return True
    if isinstance(entry.port, str):
        if named_port_target is None:
            return False
        resolved = _resolve_named_port(named_port_target, entry.port, protocol)
        if resolved is None:
            return False
        base = resolved
    else:
        base = entry.port
    if entry.endPort is not None:
        return base <= port <= entry.endPort
    return port == base


def ports_match(
    rule_ports: tuple[NetworkPolicyPort, ...],
    protocol: str,
    port: int,
    named_port_target: Optional[Pod],
) -> bool:
    """A rule with no ``ports`` field allows all ports/protocols."""
    if not rule_ports:
        return True
    return any(
        port_entry_matches(p, protocol, port, named_port_target) for p in rule_ports
    )


# ---------------------------------------------------------------------------
# Policy bookkeeping
# ---------------------------------------------------------------------------

def effective_policy_types(policy: NetworkPolicy) -> frozenset[str]:
    """Apply Kubernetes defaulting for ``policyTypes``."""
    spec = policy.spec
    if spec.policyTypes is not None:
        return frozenset(t.capitalize() for t in spec.policyTypes)
    types = {"Ingress"}
    if spec.egress is not None:
        types.add("Egress")
    return frozenset(types)


def _policies_selecting(snapshot: Snapshot, pod: Pod, direction: str):
    """Yield policies that select ``pod`` and govern ``direction``."""
    for policy in snapshot.policies:
        if policy.metadata.namespace != pod.namespace:
            continue
        if direction not in effective_policy_types(policy):
            continue
        if selector_matches(policy.spec.podSelector, pod.labels):
            yield policy


# ---------------------------------------------------------------------------
# Reachability analysis
# ---------------------------------------------------------------------------

def _evaluate_direction(
    snapshot: Snapshot,
    subject: Pod,
    peer_pod: Pod,
    direction: str,  # "Ingress" or "Egress"
    protocol: str,
    port: int,
    named_port_target: Pod,
    ns_labels: dict[str, dict[str, str]],
) -> dict:
    """Evaluate one direction for ``subject`` talking to ``peer_pod``."""
    matching_policies = list(_policies_selecting(snapshot, subject, direction))
    isolated = bool(matching_policies)
    evidence = []

    for policy in matching_policies:
        spec = policy.spec
        rules = spec.ingress if direction == "Ingress" else spec.egress
        rules = rules or ()  # missing field under an active type => deny all
        for rule_idx, rule in enumerate(rules):
            peers = rule.from_ if direction == "Ingress" else rule.to
            # A rule with no peers allows all sources/destinations.
            peer_checks = peers if peers else (None,)
            for peer_idx, peer in enumerate(peer_checks):
                if peer is not None:
                    peer_ns_labels = ns_labels.get(peer_pod.namespace, {})
                    if not peer_matches_pod(
                        peer, peer_pod, peer_ns_labels, policy.metadata.namespace
                    ):
                        continue
                if not ports_match(rule.ports, protocol, port, named_port_target):
                    continue
                evidence.append(
                    {
                        "policy": f"{policy.metadata.namespace}/{policy.metadata.name}",
                        "ruleIndex": rule_idx,
                        "peerIndex": None if peer is None else peer_idx,
                        "peer": None if peer is None else peer.model_dump(by_alias=True),
                    }
                )

    return {
        "isolated": isolated,
        "allowed": bool(evidence) if isolated else True,
        "evidence": evidence,
    }


def _resolve_query_port(query: ReachabilityQuery, dst_pod: Pod, protocol: str):
    """Resolve the query port to a number; named ports resolve on the destination."""
    if isinstance(query.port, int):
        return query.port, None
    resolved = _resolve_named_port(dst_pod, query.port, protocol)
    if resolved is None:
        return None, (
            f"named port {query.port!r} with protocol {protocol} is not declared "
            f"by destination pod {dst_pod.namespace}/{dst_pod.name}"
        )
    return resolved, None


def analyze(snapshot: Snapshot, query: ReachabilityQuery) -> dict:
    """Run a reachability query against an immutable snapshot."""
    protocol = query.protocol.upper()
    if protocol not in SUPPORTED_PROTOCOLS:
        return {
            "status": "unsupported_protocol",
            "reachable": False,
            "protocol": query.protocol,
            "detail": (
                f"protocol {query.protocol!r} is not supported; "
                f"supported protocols: {', '.join(SUPPORTED_PROTOCOLS)}"
            ),
        }

    pods = {(p.namespace, p.name): p for p in snapshot.pods}
    src = pods.get((query.source.namespace, query.source.name))
    dst = pods.get((query.destination.namespace, query.destination.name))
    missing = [
        f"{ns}/{name}"
        for (ns, name), pod in
        [((query.source.namespace, query.source.name), src),
         ((query.destination.namespace, query.destination.name), dst)]
        if pod is None
    ]
    if missing:
        raise AnalysisError(f"pod(s) not found in snapshot: {', '.join(missing)}")

    port, error = _resolve_query_port(query, dst, protocol)
    if error is not None:
        return {
            "status": "unresolved_named_port",
            "reachable": False,
            "protocol": protocol,
            "detail": error,
        }

    ns_labels = {ns.name: ns.labels for ns in snapshot.namespaces}

    egress = _evaluate_direction(
        snapshot, src, dst, "Egress", protocol, port, dst, ns_labels
    )
    ingress = _evaluate_direction(
        snapshot, dst, src, "Ingress", protocol, port, dst, ns_labels
    )

    reachable = egress["allowed"] and ingress["allowed"]
    return {
        "status": "ok",
        "reachable": reachable,
        "protocol": protocol,
        "port": port,
        "source": f"{src.namespace}/{src.name}",
        "destination": f"{dst.namespace}/{dst.name}",
        "egress": egress,
        "ingress": ingress,
        "explanation": _explain(reachable, egress, ingress),
    }


def _explain(reachable: bool, egress: dict, ingress: dict) -> str:
    if reachable:
        return "reachable: source egress and destination ingress both allow the traffic"
    reasons = []
    if not egress["allowed"]:
        reasons.append(
            "source egress denies the traffic"
            if egress["isolated"]
            else "source egress unexpectedly closed"
        )
    if not ingress["allowed"]:
        reasons.append(
            "destination ingress denies the traffic"
            if ingress["isolated"]
            else "destination ingress unexpectedly closed"
        )
    return "not reachable: " + "; ".join(reasons)
