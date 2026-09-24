"""The reachability engine.

Semantics implemented (matching Kubernetes NetworkPolicy behavior):

* Each network policy selects a set of pods via ``podSelector``. A selected
  pod is **isolated** for every direction listed in the policy's effective
  ``policyTypes``. Multiple policies isolate the same pod independently.
* Effective policyTypes: when the field is absent it defaults to
  ``["Ingress"]`` plus ``"Egress"`` when egress rules exist; when it is present
  (even empty) it is taken literally — hence empty ``policyTypes: []`` isolates
  nothing.
* A pod with no isolating policy in a direction is non-isolated there, and all
  traffic in that direction is allowed.
* For an isolated pod, traffic is allowed when at least one rule of one
  selecting policy accepts it: the peer set must include the other endpoint
  AND the port set must include the (protocol, port). Multiple policies/rules
  form a **union** (any rule may allow).
* Reachability requires **both sides**: the source pod's egress and the
  destination pod's ingress must accept.
* Peers: empty peer ``{}`` matches all pods in the policy's own namespace;
  ``podSelector`` / ``namespaceSelector`` default independently to "this
  namespace" when omitted; ``ipBlock`` matches IPs by CIDR with ``except``
  exclusions and cannot be mixed with the selectors.
* Ports: omitted ``ports`` allows all ports; numeric ports, numeric port ranges
  (``endPort``, inclusive) and named ports (resolved against the destination
  container ports) are supported.
* SCTP and any other protocol are rejected as unsupported rather than guessed.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Mapping, Sequence

from .models import (
    KNOWN_UNSUPPORTED_PROTOCOLS,
    SUPPORTED_PROTOCOLS,
    ContainerPort,
    EndpointRef,
    IPBlock,
    NetworkPolicy,
    NetworkPolicyPeer,
    NetworkPolicyPort,
    Pod,
    QueryRequest,
    Snapshot,
)
from .selectors import selector_matches

WILDCARD_PEER = "__wildcard__"  # empty peer {}: pods in policy's namespace
RULE_WILDCARD = "__rule_wildcard__"  # missing from/to: ALL endpoints incl. IPs


class UnsupportedProtocolError(Exception):
    """Raised when a query/evaluation targets a protocol we cannot model."""


class ValidationError(Exception):
    """Snapshot or query input is inconsistent."""


# --------------------------------------------------------------------------- #
# Resolved endpoints
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Endpoint:
    kind: str  # "pod" | "ip"
    namespace: str | None
    labels: Mapping[str, str]
    ns_labels: Mapping[str, str]
    ips: tuple[str, ...]
    container_ports: tuple[ContainerPort, ...]
    ref: str  # human-readable identity, e.g. "default/client" or "10.1.2.3"


@dataclass(frozen=True)
class MatchedPort:
    rule_index: int
    entry_index: int
    protocol: str
    ports: frozenset[int]
    named_port: str | None
    port_range: tuple[int, int] | None
    wildcard: bool
    unsupported: bool


@dataclass(frozen=True)
class PeerMatch:
    rule_index: int
    entry_index: int
    kind: str  # WILDCARD_PEER | "selectors" | "ipBlock"
    detail: Mapping[str, object]


# --------------------------------------------------------------------------- #
# Snapshot validation / indexing
# --------------------------------------------------------------------------- #


def validate_snapshot(snapshot: Snapshot) -> list[str]:
    """Cross-field validation. Returns human-readable warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    ns_names = [n.name for n in snapshot.namespaces]
    if len(ns_names) != len(set(ns_names)):
        errors.append("duplicate namespace name in snapshot")
    ns_set = set(ns_names)
    # `default` always exists implicitly in a cluster.
    ns_set.add("default")

    pod_keys = [(p.namespace, p.name) for p in snapshot.pods]
    if len(pod_keys) != len(set(pod_keys)):
        errors.append("duplicate (namespace, name) pod key in snapshot")
    for ns, name in pod_keys:
        if ns not in ns_set:
            errors.append(f"pod {ns}/{name} references unknown namespace {ns!r}")

    pol_keys = [(p.namespace, p.name) for p in snapshot.policies]
    if len(pol_keys) != len(set(pol_keys)):
        errors.append("duplicate (namespace, name) policy key in snapshot")
    for ns, name in pol_keys:
        if ns not in ns_set:
            errors.append(
                f"policy {ns}/{name} references unknown namespace {ns!r}"
            )

    for pol in snapshot.policies:
        for rule in pol.ingress or []:
            for p in rule.ports or ():
                if p.protocol in KNOWN_UNSUPPORTED_PROTOCOLS:
                    warnings.append(
                        f"policy {pol.namespace}/{pol.name} ingress rule uses "
                        f"unsupported protocol {p.protocol} (port "
                        f"{p.port if p.port is not None else 'ANY'})"
                    )
        for rule in pol.egress or ():
            for p in rule.ports or ():
                if p.protocol in KNOWN_UNSUPPORTED_PROTOCOLS:
                    warnings.append(
                        f"policy {pol.namespace}/{pol.name} egress rule uses "
                        f"unsupported protocol {p.protocol} (port "
                        f"{p.port if p.port is not None else 'ANY'})"
                    )

    if errors:
        raise ValidationError("; ".join(errors))
    return warnings


class Analyzer:
    """Holds an immutable snapshot and evaluates reachability queries."""

    def __init__(self, snapshot: Snapshot):
        self._warnings = validate_snapshot(snapshot)
        self._snapshot = snapshot
        self._namespaces: Mapping[str, Mapping[str, str]] = {
            n.name: dict(n.labels) for n in snapshot.namespaces
        }
        self._pods: dict[tuple[str, str], Pod] = {
            (p.namespace, p.name): p for p in snapshot.pods
        }

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    # ------------------------------------------------------------------ #
    # Endpoint resolution
    # ------------------------------------------------------------------ #

    def resolve_endpoint(self, ref: EndpointRef) -> Endpoint:
        if ref.ip is not None:
            return Endpoint(
                kind="ip",
                namespace=None,
                labels={},
                ns_labels={},
                ips=(ref.ip,),
                container_ports=(),
                ref=ref.ip,
            )
        ns = ref.pod.get("namespace", "default")
        name = ref.pod["name"]
        pod = self._pods.get((ns, name))
        if pod is None:
            raise ValidationError(f"endpoint pod not found in snapshot: {ns}/{name}")
        return Endpoint(
            kind="pod",
            namespace=pod.namespace,
            labels=dict(pod.labels),
            ns_labels=dict(self._namespaces.get(pod.namespace, {})),
            ips=tuple(pod.ips),
            container_ports=tuple(pod.container_ports),
            ref=f"{pod.namespace}/{pod.name}",
        )

    # ------------------------------------------------------------------ #
    # Policy selection and effective policyTypes
    # ------------------------------------------------------------------ #

    @staticmethod
    def _effective_policy_types(pol: NetworkPolicy) -> frozenset[str]:
        if pol.policy_types is not None:
            # Present (possibly empty): used literally. [] isolates nothing.
            return frozenset(pol.policy_types)
        result = {"Ingress"}
        if pol.egress:
            result.add("Egress")
        return frozenset(result)

    def _selecting_policies(self, pod: Endpoint, direction: str):
        if pod.kind != "pod":
            return ()
        out = []
        for pol in self._snapshot.policies:
            types = self._effective_policy_types(pol)
            if direction not in types:
                continue
            # podSelector selects within the policy's own namespace.
            if pod.namespace != pol.namespace:
                continue
            if selector_matches(pol.pod_selector, pod.labels):
                out.append(pol)
        return tuple(out)

    # ------------------------------------------------------------------ #
    # Peer matching
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ip_in_block(block: IPBlock, endpoint: Endpoint) -> bool:
        net = ipaddress.ip_network(block.cidr)
        excluded = [ipaddress.ip_network(e) for e in block.except_]
        for raw in endpoint.ips:
            ip = ipaddress.ip_address(raw)
            if ip.version != net.version or ip not in net:
                continue
            if any(ip.version == ex.version and ip in ex for ex in excluded):
                continue
            return True
        return False

    def _peer_matches(
        self,
        peer: NetworkPolicyPeer,
        policy_ns: str,
        endpoint: Endpoint,
    ) -> tuple[bool, str, dict]:
        if peer.ip_block is not None:
            ok = self._ip_in_block(peer.ip_block, endpoint)
            return ok, "ipBlock", {
                "cidr": peer.ip_block.cidr,
                "except": list(peer.ip_block.except_),
            }

        # Empty peer {} => all pods in the policy's own namespace (not IPs,
        # not other namespaces). A missing from/to list at the rule level is
        # different — that matches every endpoint; see _rule_peers.
        if peer.pod_selector is None and peer.namespace_selector is None:
            ok = endpoint.kind == "pod" and endpoint.namespace == policy_ns
            return ok, WILDCARD_PEER, {"scope": "policy-namespace"}

        # namespaceSelector: missing => the policy's own namespace.
        if peer.namespace_selector is None:
            if endpoint.kind != "pod" or endpoint.namespace != policy_ns:
                return False, "selectors", {}
            ns_ok = True
            pod_sel_ok = (
                True
                if peer.pod_selector is None
                else selector_matches(peer.pod_selector, endpoint.labels)
            )
            return ns_ok and pod_sel_ok, "selectors", {}

        ns_ok = selector_matches(peer.namespace_selector, endpoint.ns_labels)
        if not ns_ok:
            return False, "selectors", {}
        if peer.pod_selector is None:
            # namespaceSelector alone selects ALL pods of matching namespaces.
            return endpoint.kind == "pod", "selectors", {}
        if endpoint.kind != "pod":
            return False, "selectors", {}
        pod_sel_ok = selector_matches(peer.pod_selector, endpoint.labels)
        return pod_sel_ok, "selectors", {}

    # ------------------------------------------------------------------ #
    # Port matching
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_named_ports(
        entry: NetworkPolicyPort,
        target: Endpoint,
    ) -> tuple[frozenset[int], bool]:
        """Resolve one ports[] entry against the destination's container ports.

        Returns (numeric ports, unresolved). ``unresolved`` means the named
        port does not exist on the target pods (the rule grants nothing).
        """
        if entry.port is None:
            return frozenset(), True  # wildcard, caller handles
        if isinstance(entry.port, int):
            if entry.end_port is not None:
                return frozenset(range(entry.port, entry.end_port + 1)), False
            return frozenset({entry.port}), False
        # named port: resolve against destination container ports, same proto
        hits = frozenset(
            cp.container_port
            for cp in target.container_ports
            if cp.name == entry.port and cp.protocol == entry.protocol
        )
        return hits, len(hits) == 0

    def _port_entries(
        self,
        ports: Sequence[NetworkPolicyPort] | None,
        protocol: str,
        port: int | None,
        target: Endpoint,
        rule_index: int,
    ) -> list[MatchedPort]:
        """Return the ports[] entries that grant (protocol, port)."""
        if ports is None:
            return [
                MatchedPort(
                    rule_index=rule_index,
                    entry_index=-1,
                    protocol=protocol,
                    ports=frozenset(),
                    named_port=None,
                    port_range=None,
                    wildcard=True,
                    unsupported=False,
                )
            ]
        out: list[MatchedPort] = []
        for i, entry in enumerate(ports):
            if entry.protocol in KNOWN_UNSUPPORTED_PROTOCOLS:
                continue  # cannot grant traffic for a protocol we don't model
            if entry.protocol != protocol:
                continue
            wildcard = entry.port is None
            if wildcard:
                # An entry that names only a protocol ("ports":[{"protocol":
                # "TCP"}]) grants EVERY port of that protocol.
                out.append(
                    MatchedPort(
                        rule_index=rule_index,
                        entry_index=i,
                        protocol=entry.protocol,
                        ports=frozenset(),
                        named_port=None,
                        port_range=None,
                        wildcard=True,
                        unsupported=False,
                    )
                )
                continue
            resolved, unresolved = self._resolve_named_ports(entry, target)
            if unresolved:
                continue
            rng = (
                (entry.port, entry.end_port)
                if isinstance(entry.port, int) and entry.end_port is not None
                else None
            )
            if port is not None and port not in resolved:
                continue
            out.append(
                MatchedPort(
                    rule_index=rule_index,
                    entry_index=i,
                    protocol=entry.protocol,
                    ports=resolved,
                    named_port=entry.port if isinstance(entry.port, str) else None,
                    port_range=rng,
                    wildcard=False,
                    unsupported=False,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Per-direction evaluation
    # ------------------------------------------------------------------ #

    def _evaluate_direction(
        self,
        *,
        policies: Sequence[NetworkPolicy],
        peer_target: Endpoint,
        port_target: Endpoint,
        rules_getter,
        protocol: str,
        port: int | None,
        direction: str,
        target_ref: str,
    ) -> dict:
        """Evaluate one side (egress of source pod or ingress of dest pod).

        ``peer_target`` is the remote endpoint matched by from/to peers;
        ``port_target`` is the endpoint whose container ports resolve named
        ports (always the traffic destination).
        """
        if not policies:
            return {
                "isolated": False,
                "allowed": True,
                "reason": f"no policy isolates {target_ref} for {direction.lower()}",
                "selectingPolicies": [],
                "allowedBy": [],
            }

        selecting = [
            {
                "namespace": pol.namespace,
                "name": pol.name,
                "ruleCount": len(rules_getter(pol) or ()),
            }
            for pol in policies
        ]
        allowed_by: list[dict] = []

        for pol in policies:
            rules = rules_getter(pol) or ()
            for rule_index, rule in enumerate(rules):
                peers = self._rule_peers(rule, direction)
                port_entries = self._port_entries(
                    rule.ports, protocol, port, port_target, rule_index
                )
                if not port_entries:
                    continue
                for entry_index, (marker, peer) in enumerate(peers):
                    if marker == RULE_WILDCARD:
                        ok, kind, detail = True, RULE_WILDCARD, {
                            "scope": "all-endpoints"
                        }
                    else:
                        ok, kind, detail = self._peer_matches(
                            peer, pol.namespace, peer_target
                        )
                    if not ok:
                        continue
                    for mp in port_entries:
                        allowed_by.append(
                            self._evidence(
                                pol, direction, rule_index, entry_index, kind,
                                detail, mp,
                            )
                        )

        allowed = len(allowed_by) > 0
        reason = (
            f"{len(allowed_by)} matching rule(s) allow the traffic"
            if allowed
            else f"{target_ref} is isolated for {direction.lower()} and no rule "
            "allows this peer/port combination"
        )
        return {
            "isolated": True,
            "allowed": allowed,
            "reason": reason,
            "selectingPolicies": selecting,
            "allowedBy": allowed_by,
        }

    @staticmethod
    def _rule_peers(rule, direction: str):
        """Return ``(marker, peer_or_None)`` entries for a rule.

        * Missing from/to => one rule-wildcard entry matching EVERY endpoint,
          including external IPs (Kubernetes semantics).
        * Empty list ``[]`` => no entries: the rule matches nobody.
        * Otherwise => the explicit peers (an empty peer ``{}`` among them
          matches only pods of the policy's own namespace).
        """
        raw = rule.from_ if direction == "Ingress" else rule.to
        if raw is None:
            return [(RULE_WILDCARD, None)]
        return [("peer", peer) for peer in raw]

    @staticmethod
    def _evidence(
        pol: NetworkPolicy,
        direction: str,
        rule_index: int,
        entry_index: int,
        kind: str,
        detail: Mapping[str, object],
        mp: MatchedPort,
    ) -> dict:
        if mp.wildcard:
            port_field = "ANY"
        elif mp.named_port is not None:
            port_field = f"named:{mp.named_port}"
        elif mp.port_range is not None:
            lo, hi = mp.port_range
            port_field = f"{lo}-{hi}"
        else:
            port_field = ",".join(str(p) for p in sorted(mp.ports))
        if kind == RULE_WILDCARD:
            peer_field = "wildcard(all-endpoints)"
        elif kind == WILDCARD_PEER:
            peer_field = "wildcard(policy-namespace)"
        else:
            peer_field = kind
        return {
            "policy": {"namespace": pol.namespace, "name": pol.name},
            "direction": direction.lower(),
            "ruleIndex": rule_index,
            "peerIndex": entry_index,
            "peer": peer_field,
            "peerDetail": dict(detail),
            "protocol": mp.protocol,
            "allowedPorts": port_field,
        }

    # ------------------------------------------------------------------ #
    # Public query
    # ------------------------------------------------------------------ #

    def analyze(self, query: QueryRequest) -> dict:
        if query.protocol not in SUPPORTED_PROTOCOLS:
            raise UnsupportedProtocolError(
                f"protocol {query.protocol!r} is not supported by this analyzer; "
                f"supported: {sorted(SUPPORTED_PROTOCOLS)}"
            )

        source = self.resolve_endpoint(query.source)
        dest = self.resolve_endpoint(query.destination)

        # Only pod endpoints carry NetworkPolicy isolation. External IPs have
        # no egress policies; non-pod sources have no ingress restrictions.
        egress_policies = self._selecting_policies(source, "Egress")
        ingress_policies = self._selecting_policies(dest, "Ingress")

        egress_result = self._evaluate_direction(
            policies=egress_policies,
            peer_target=dest,
            port_target=dest,
            rules_getter=lambda p: p.egress,
            protocol=query.protocol,
            port=query.port,
            direction="Egress",
            target_ref=source.ref,
        )
        ingress_result = self._evaluate_direction(
            policies=ingress_policies,
            peer_target=source,
            port_target=dest,
            rules_getter=lambda p: p.ingress,
            protocol=query.protocol,
            port=query.port,
            direction="Ingress",
            target_ref=dest.ref,
        )

        reachable = egress_result["allowed"] and ingress_result["allowed"]
        return {
            "query": {
                "source": source.ref,
                "destination": dest.ref,
                "protocol": query.protocol,
                "port": query.port if query.port is not None else "ANY",
            },
            "reachable": reachable,
            "reason": self._overall_reason(
                reachable, egress_result, ingress_result
            ),
            "egress": egress_result,
            "ingress": ingress_result,
        }

    @staticmethod
    def _overall_reason(reachable: bool, egress: dict, ingress: dict) -> str:
        if reachable:
            return "egress and ingress both allow the traffic"
        blockers = []
        if not egress["allowed"]:
            blockers.append("source egress denied")
        if not ingress["allowed"]:
            blockers.append("destination ingress denied")
        return "traffic blocked: " + " and ".join(blockers)
