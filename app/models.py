"""Immutable snapshot and query models.

All models are frozen pydantic models: once a snapshot is parsed it cannot be
mutated, which guarantees the analyzer always works on the exact input it was
given.  Missing fields are kept distinct from explicitly empty values
(``None`` vs ``[]`` / ``{}``) because Kubernetes NetworkPolicy semantics
depend on that difference:

* ``podSelector: {}`` selects **all** pods in the policy namespace, while a
  missing ``podSelector`` is normalised to ``{}`` by Kubernetes as well but is
  tracked separately here for evidence reporting.
* ``ingress: []`` (present but empty) isolates selected pods and denies all
  ingress traffic, while a missing ``ingress`` field means the policy does not
  govern ingress at all.
"""

from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Label selectors
# ---------------------------------------------------------------------------

class MatchExpression(FrozenModel):
    key: str
    operator: str  # In | NotIn | Exists | DoesNotExist
    values: tuple[str, ...] = ()


class LabelSelector(FrozenModel):
    """A Kubernetes label selector.

    An empty selector (``{}``) matches **everything**; a ``None`` selector is
    handled by the caller and means "not specified".
    """

    matchLabels: dict[str, str] = Field(default_factory=dict)
    matchExpressions: tuple[MatchExpression, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.matchLabels and not self.matchExpressions


# ---------------------------------------------------------------------------
# NetworkPolicy spec
# ---------------------------------------------------------------------------

class IPBlock(FrozenModel):
    cidr: str
    except_: tuple[str, ...] = Field(default_factory=tuple, alias="except")


class NetworkPolicyPeer(FrozenModel):
    podSelector: Optional[LabelSelector] = None
    namespaceSelector: Optional[LabelSelector] = None
    ipBlock: Optional[IPBlock] = None


class NetworkPolicyPort(FrozenModel):
    protocol: str = "TCP"
    port: Optional[Union[int, str]] = None
    endPort: Optional[int] = None


class IngressRule(FrozenModel):
    from_: tuple[NetworkPolicyPeer, ...] = Field(default_factory=tuple, alias="from")
    ports: tuple[NetworkPolicyPort, ...] = ()


class EgressRule(FrozenModel):
    to: tuple[NetworkPolicyPeer, ...] = ()
    ports: tuple[NetworkPolicyPort, ...] = ()


class NetworkPolicySpec(FrozenModel):
    # Missing podSelector is normalised to an empty selector (selects all pods
    # in the policy namespace), matching Kubernetes API defaulting.
    podSelector: LabelSelector = Field(default_factory=LabelSelector)
    ingress: Optional[tuple[IngressRule, ...]] = None
    egress: Optional[tuple[EgressRule, ...]] = None
    policyTypes: Optional[tuple[str, ...]] = None


class ObjectMeta(FrozenModel):
    name: str
    namespace: str = "default"


class NetworkPolicy(FrozenModel):
    metadata: ObjectMeta
    spec: NetworkPolicySpec


# ---------------------------------------------------------------------------
# Cluster state
# ---------------------------------------------------------------------------

class ContainerPort(FrozenModel):
    name: Optional[str] = None
    containerPort: int
    protocol: str = "TCP"


class Pod(FrozenModel):
    name: str
    namespace: str = "default"
    labels: dict[str, str] = Field(default_factory=dict)
    ip: Optional[str] = None
    ports: tuple[ContainerPort, ...] = ()


class Namespace(FrozenModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)


class Snapshot(FrozenModel):
    """An immutable view of the cluster at one point in time."""

    namespaces: tuple[Namespace, ...] = ()
    pods: tuple[Pod, ...] = ()
    policies: tuple[NetworkPolicy, ...] = ()


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

class PodRef(FrozenModel):
    namespace: str
    name: str


class ReachabilityQuery(FrozenModel):
    source: PodRef
    destination: PodRef
    protocol: str = "TCP"
    port: Union[int, str]
