"""Immutable domain models for cluster snapshots, policies and queries.

The model layer deliberately distinguishes *missing* fields (``None``) from
*present-but-empty* fields (``{}`` / ``[]``). Kubernetes semantics treat these
differently in several places (notably ``policyTypes``), and the analyzer
relies on that distinction, so plain dict defaulting would erase information.
"""

from __future__ import annotations

import ipaddress
from typing import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Protocols the reachability engine can actually evaluate.
SUPPORTED_PROTOCOLS = frozenset({"TCP", "UDP"})
# Protocols Kubernetes accepts but this analyzer does not evaluate.
KNOWN_UNSUPPORTED_PROTOCOLS = frozenset({"SCTP"})
VALID_PROTOCOLS = SUPPORTED_PROTOCOLS | KNOWN_UNSUPPORTED_PROTOCOLS
DIRECTIONS = frozenset({"Ingress", "Egress"})
SELECTOR_OPERATORS = frozenset({"In", "NotIn", "Exists", "DoesNotExist"})


class FrozenModel(BaseModel):
    """Base class: instances are immutable, unknown keys rejected."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
    )


def _validate_label_key(key: str) -> str:
    if not key or len(key) > 313:
        raise ValueError(f"invalid label key {key!r}: must be 1..313 characters")
    if "/" in key:
        prefix, _, name = key.partition("/")
        if not prefix or not name or "/" in name:
            raise ValueError(f"invalid label key {key!r}: bad prefix/name split")
        key = name  # validate the name portion charset below
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if not all(c in allowed for c in key):
        raise ValueError(f"invalid label key {key!r}: illegal character")
    if key in ("-", "_", ".") or key[0] in "-_." or key[-1] in "-_.":
        raise ValueError(f"invalid label key {key!r}: bad leading/trailing character")
    return key


class LabelSelectorRequirement(FrozenModel):
    """One ``matchExpressions`` entry (set-based selector requirement)."""

    key: str
    operator: str
    values: Sequence[str] = ()

    @field_validator("key")
    @classmethod
    def _key_ok(cls, v: str) -> str:
        return _validate_label_key(v)

    @field_validator("operator")
    @classmethod
    def _op_ok(cls, v: str) -> str:
        if v not in SELECTOR_OPERATORS:
            raise ValueError(
                f"invalid operator {v!r}: must be one of {sorted(SELECTOR_OPERATORS)}"
            )
        return v

    @model_validator(mode="after")
    def _values_shape(self) -> "LabelSelectorRequirement":
        if self.operator in ("In", "NotIn") and len(self.values) == 0:
            raise ValueError(f"operator {self.operator!r} requires at least one value")
        if self.operator in ("Exists", "DoesNotExist") and len(self.values) != 0:
            raise ValueError(f"operator {self.operator!r} takes no values")
        if len(set(self.values)) != len(self.values):
            raise ValueError("requirement values must be unique")
        return self


class LabelSelector(FrozenModel):
    """A label selector.

    An *empty* selector (``{}``, i.e. no labels and no expressions) matches
    every object. Field *absence* is represented one level up by ``None``
    (e.g. ``NetworkPolicyPeer.pod_selector is None``).
    """

    match_labels: Mapping[str, str] = Field(default_factory=dict, alias="matchLabels")
    match_expressions: Sequence[LabelSelectorRequirement] = Field(
        default_factory=list, alias="matchExpressions"
    )

    @field_validator("match_labels")
    @classmethod
    def _labels_ok(cls, v: Mapping[str, str]) -> Mapping[str, str]:
        for k, val in v.items():
            _validate_label_key(k)
            if not isinstance(val, str) or not val:
                raise ValueError(f"label value for {k!r} must be a non-empty string")
            if len(val) > 63:
                raise ValueError(f"label value for {k!r} exceeds 63 characters")
        return dict(v)

    @field_validator("match_expressions")
    @classmethod
    def _exprs_ok(cls, v: Sequence[LabelSelectorRequirement]) -> tuple:
        return tuple(v)


class IPBlock(FrozenModel):
    """An ``ipBlock`` peer: a CIDR with optional ``except`` exclusions."""

    cidr: str
    except_: Sequence[str] = Field(default_factory=list, alias="except")

    @field_validator("cidr")
    @classmethod
    def _cidr_ok(cls, v: str) -> str:
        try:
            return str(ipaddress.ip_network(v, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid CIDR {v!r}: {exc}") from exc

    @field_validator("except_")
    @classmethod
    def _except_ok(cls, v: Sequence[str]) -> tuple:
        nets = []
        for entry in v:
            try:
                nets.append(ipaddress.ip_network(entry, strict=False))
            except ValueError as exc:
                raise ValueError(f"invalid except CIDR {entry!r}: {exc}") from exc
        for i, a in enumerate(nets):
            for b in nets[i + 1 :]:
                if a.version == b.version and (a.subnet_of(b) or b.subnet_of(a)):
                    raise ValueError(
                        f"overlapping except CIDRs: {a} and {b}"
                    )
        return tuple(str(n) for n in nets)

    @model_validator(mode="after")
    def _except_within(self) -> "IPBlock":
        outer = ipaddress.ip_network(self.cidr)
        for entry in self.except_:
            net = ipaddress.ip_network(entry)
            if net.version != outer.version or not net.subnet_of(outer):
                raise ValueError(f"except CIDR {entry} is not within {self.cidr}")
        return self


class NetworkPolicyPort(FrozenModel):
    """One port rule entry (``ports[]``)."""

    port: int | str | None = None
    end_port: int | None = Field(default=None, alias="endPort")
    protocol: str = "TCP"

    @field_validator("protocol")
    @classmethod
    def _proto_ok(cls, v: str) -> str:
        up = v.upper()
        if up not in VALID_PROTOCOLS:
            raise ValueError(
                f"invalid protocol {v!r}: supported values are "
                f"{sorted(VALID_PROTOCOLS)}"
            )
        return up

    @field_validator("port")
    @classmethod
    def _port_ok(cls, v):
        if isinstance(v, int) and not (1 <= v <= 65535):
            raise ValueError(f"numeric port {v} out of range 1..65535")
        if isinstance(v, str) and not v:
            raise ValueError("named port must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _endport_shape(self) -> "NetworkPolicyPort":
        if self.end_port is not None:
            if not isinstance(self.port, int):
                raise ValueError("endPort requires a numeric port")
            if not (1 <= self.end_port <= 65535):
                raise ValueError("endPort out of range 1..65535")
            if self.end_port < self.port:
                raise ValueError("endPort must be greater than or equal to port")
        return self


class NetworkPolicyPeer(FrozenModel):
    """One ``from[]`` / ``to[]`` entry.

    All three selectors may be ``None``: an empty peer object (``{}``) selects
    all pods in the policy's own namespace. ``ipBlock`` cannot be combined with
    the pod/namespace selectors (Kubernetes IPBlockExclusion rule).
    """

    pod_selector: LabelSelector | None = Field(default=None, alias="podSelector")
    namespace_selector: LabelSelector | None = Field(
        default=None, alias="namespaceSelector"
    )
    ip_block: IPBlock | None = Field(default=None, alias="ipBlock")

    @model_validator(mode="after")
    def _no_ipblock_mix(self) -> "NetworkPolicyPeer":
        if self.ip_block is not None and (
            self.pod_selector is not None or self.namespace_selector is not None
        ):
            raise ValueError("ipBlock cannot be combined with pod/namespace selectors")
        return self


class IngressRule(FrozenModel):
    from_: Sequence[NetworkPolicyPeer] | None = Field(default=None, alias="from")
    ports: Sequence[NetworkPolicyPort] | None = None

    @field_validator("from_", "ports")
    @classmethod
    def _freeze_seq(cls, v):
        return None if v is None else tuple(v)


class EgressRule(FrozenModel):
    to: Sequence[NetworkPolicyPeer] | None = None
    ports: Sequence[NetworkPolicyPort] | None = None

    @field_validator("to", "ports")
    @classmethod
    def _freeze_seq(cls, v):
        return None if v is None else tuple(v)


class NetworkPolicy(FrozenModel):
    """A Kubernetes NetworkPolicy in a simplified, analysis-friendly shape."""

    name: str
    namespace: str = "default"
    pod_selector: LabelSelector = Field(alias="podSelector")
    policy_types: Sequence[str] | None = Field(default=None, alias="policyTypes")
    ingress: Sequence[IngressRule] | None = None
    egress: Sequence[EgressRule] | None = None

    @field_validator("name", "namespace")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("name and namespace must be non-empty")
        return v

    @field_validator("policy_types")
    @classmethod
    def _types_ok(cls, v):
        if v is None:
            return None
        up = tuple(d.capitalize() for d in v)
        for d in up:
            if d not in DIRECTIONS:
                raise ValueError(f"invalid policyType {d!r}: use Ingress/Egress")
        if len(set(up)) != len(up):
            raise ValueError("policyTypes entries must be unique")
        return up

    @field_validator("ingress", "egress")
    @classmethod
    def _freeze_rules(cls, v):
        return None if v is None else tuple(v)


class ContainerPort(FrozenModel):
    """A container port on a pod; named ports resolve against these."""

    name: str | None = None
    container_port: int = Field(alias="containerPort")
    protocol: str = "TCP"

    @field_validator("protocol")
    @classmethod
    def _proto_ok(cls, v: str) -> str:
        up = v.upper()
        if up not in VALID_PROTOCOLS:
            raise ValueError(f"invalid container port protocol {v!r}")
        return up

    @field_validator("container_port")
    @classmethod
    def _port_ok(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("containerPort out of range 1..65535")
        return v

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v):
        if v is not None and not v:
            raise ValueError("container port name must be non-empty")
        return v


class Pod(FrozenModel):
    name: str
    namespace: str = "default"
    labels: Mapping[str, str] = Field(default_factory=dict)
    ips: Sequence[str] = Field(default_factory=list)
    container_ports: Sequence[ContainerPort] = Field(
        default_factory=list, alias="containerPorts"
    )

    @field_validator("name", "namespace")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("pod name and namespace must be non-empty")
        return v

    @field_validator("labels")
    @classmethod
    def _labels_ok(cls, v: Mapping[str, str]) -> Mapping[str, str]:
        for k, val in v.items():
            _validate_label_key(k)
            if not isinstance(val, str):
                raise ValueError(f"label value for {k!r} must be a string")
        return dict(v)

    @field_validator("ips")
    @classmethod
    def _ips_ok(cls, v: Sequence[str]) -> tuple:
        out = []
        for ip in v:
            try:
                out.append(str(ipaddress.ip_address(ip)))
            except ValueError as exc:
                raise ValueError(f"invalid pod IP {ip!r}: {exc}") from exc
        if len(set(out)) != len(out):
            raise ValueError("duplicate pod IPs")
        return tuple(out)

    @field_validator("container_ports")
    @classmethod
    def _ports_freeze(cls, v):
        return tuple(v)


class Namespace(FrozenModel):
    name: str
    labels: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("namespace name must be non-empty")
        return v

    @field_validator("labels")
    @classmethod
    def _labels_ok(cls, v: Mapping[str, str]) -> Mapping[str, str]:
        for k in v:
            _validate_label_key(k)
        return dict(v)


class Snapshot(FrozenModel):
    """Immutable cluster snapshot: namespaces, pods and policies."""

    namespaces: Sequence[Namespace] = ()
    pods: Sequence[Pod] = ()
    policies: Sequence[NetworkPolicy] = ()

    @field_validator("namespaces", "pods", "policies")
    @classmethod
    def _freeze(cls, v):
        return tuple(v)


# --------------------------------------------------------------------------- #
# Query / response models
# --------------------------------------------------------------------------- #


class EndpointRef(FrozenModel):
    """A traffic endpoint: either a pod reference or a literal IP address."""

    pod: Mapping[str, str] | None = None
    ip: str | None = None

    @field_validator("pod")
    @classmethod
    def _pod_shape(cls, v):
        if v is None:
            return None
        if not v.get("name"):
            raise ValueError("pod reference requires a name")
        return dict(v)

    @field_validator("ip")
    @classmethod
    def _ip_ok(cls, v):
        if v is None:
            return None
        try:
            return str(ipaddress.ip_address(v))
        except ValueError as exc:
            raise ValueError(f"invalid endpoint IP {v!r}: {exc}") from exc

    @model_validator(mode="after")
    def _exactly_one(self) -> "EndpointRef":
        if (self.pod is None) == (self.ip is None):
            raise ValueError("endpoint must specify exactly one of pod or ip")
        return self


class QueryRequest(FrozenModel):
    source: EndpointRef = Field(alias="source")
    destination: EndpointRef = Field(alias="destination")
    protocol: str = "TCP"
    port: int | None = None

    # Batch files (examples, CLI) may carry a human-readable "name" comment.
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    @field_validator("protocol")
    @classmethod
    def _proto_ok(cls, v: str) -> str:
        return v.upper()

    @field_validator("port")
    @classmethod
    def _port_ok(cls, v):
        if v is not None and not (1 <= v <= 65535):
            raise ValueError("query port out of range 1..65535")
        return v
