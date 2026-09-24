"""PDB accounting, multi-PDB intersection admission and safety audit.

This module is pure: every function reads an in-memory :class:`Snapshot` and
never mutates the cluster.  The simulator calls :func:"admit_evictions" to
decide which pods may be evicted together in one wave; the audit helpers let
the test-suite prove that *no executed step ever violates a budget*.

Modelling notes (deliberately conservative vs. real Kubernetes):

* A pod with a deletion timestamp counts as ``disrupted`` and leaves
  ``expected/current/healthy`` until it physically disappears. Its replacement
  is counted in ``expected`` as soon as it exists (Pending or not).
* ``minAvailable`` / ``maxUnavailable`` accept absolute integers or ``"n%"``;
  percentages round *up* like ``intstr.FindPercent``/ceil in Kubernetes.
* ``unhealthyPodEvictionPolicy`` follows the 1.26+ semantics: under
  ``IfHealthyBudget`` an unhealthy pod may only be disrupted while the
  workload still meets its full healthy budget; ``AlwaysAllow`` relaxes that.
* Terminal (Succeeded/Failed) pods consume no budget.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .models import (
    DaemonSet,
    Deployment,
    Pod,
    PodDisruptionBudget,
    ReplicaSet,
    Snapshot,
)

Workload = Deployment | ReplicaSet | DaemonSet


def pdb_key(namespace: str, name: str) -> str:
    return f"{namespace}/{name}"


def labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in selector.items())


def pod_matches_pdb(pod: Pod, pdb: PodDisruptionBudget) -> bool:
    return (
        pod.namespace == pdb.namespace
        and labels_match(pdb.selector.match_labels, pod.labels)
    )


# --------------------------------------------------------------------------- #
# Workload / controller resolution
# --------------------------------------------------------------------------- #
def resolve_workload(snap: Snapshot, pod: Pod) -> Optional[tuple[str, Workload]]:
    """Return ("DaemonSet"|"Deployment"|"ReplicaSet", object) or None.

    Real pods usually reference the ReplicaSet rather than the Deployment, so
    ReplicaSet-owned pods are attributed to a Deployment either when the RS
    object is present (its name conventionally starts with the deployment
    name) or, when the ReplicaSet is absent from the snapshot, by matching the
    pod's labels against a deployment selector — the same labels the
    Deployment's ReplicaSet template carries.
    """
    if pod.owner is None:
        return None
    kind, name = pod.owner.kind, pod.owner.name
    if kind == "DaemonSet":
        for ds in snap.daemon_sets:
            if ds.namespace == pod.namespace and ds.name == name:
                return "DaemonSet", ds
        return None

    rs: Optional[ReplicaSet] = None
    if kind == "ReplicaSet":
        rs = next(
            (
                r
                for r in snap.replica_sets
                if r.namespace == pod.namespace and r.name == name
            ),
            None,
        )

    for dep in snap.deployments:
        if dep.namespace != pod.namespace:
            continue
        if not labels_match(dep.selector.match_labels, pod.labels):
            continue
        if kind == "Deployment" and dep.name == name:
            return "Deployment", dep
        if kind == "ReplicaSet" and (
            (rs is not None and (rs.name == dep.name or rs.name.startswith(dep.name + "-")))
            or rs is None  # RS not snapshotted: label selector is the evidence
        ):
            return "Deployment", dep

    if rs is not None:
        return "ReplicaSet", rs
    return None


def is_daemonset_pod(snap: Snapshot, pod: Pod) -> bool:
    w = resolve_workload(snap, pod)
    return w is not None and w[0] == "DaemonSet"


def hard_eviction_blocker(pod: Pod) -> Optional[str]:
    """A reason string if the pod can never be evicted by a drain, else None."""
    if pod.mirror:
        return "mirror pod (created by a kubelet manifest, cannot be evicted)"
    if pod.static_pod:
        return "static pod (kubelet-managed, cannot be evicted)"
    return None


# --------------------------------------------------------------------------- #
# PDB numbers
# --------------------------------------------------------------------------- #
def _required_count(value: int | str, expected: int) -> int:
    """Resolve an intstr int or 'n%' value against the expected pod count."""
    if isinstance(value, int):
        return value
    v = str(value).strip()
    if v.endswith("%"):
        pct = int(v[:-1])
        if not 0 <= pct <= 100:
            raise ValueError(f"invalid percentage {value!r}")
        return math.ceil(expected * pct / 100)
    return int(v)


@dataclass
class PdbAccounting:
    key: str
    expected: int            # E: replicas promised by controllers + bare pods
    healthy: int             # ready, Running, non-terminating
    disrupted: int           # terminating (deletionTimestamp set)
    desired_healthy: int     # admission-time floor (blocks extra admissions)
    audit_floor: int         # floor the running workload must never fall under
    disruptions_allowed: int

    def as_dict(self) -> dict[str, int]:
        return {
            "expected": self.expected,
            "healthy": self.healthy,
            "disrupted": self.disrupted,
            "desiredHealthy": self.desired_healthy,
            "auditFloor": self.audit_floor,
            "disruptionsAllowed": self.disruptions_allowed,
        }


@dataclass
class _Counts:
    pods: list[Pod] = field(default_factory=list)          # non-terminating matching
    terminating: list[Pod] = field(default_factory=list)   # terminating matching


def _counts(snap: Snapshot, pdb: PodDisruptionBudget, extra_terminating: set[str]) -> _Counts:
    c = _Counts()
    for pod in snap.pods:
        if not pod_matches_pdb(pod, pdb):
            continue
        if pod.deleting or pod.uid in extra_terminating or pod.name in extra_terminating:
            c.terminating.append(pod)
        else:
            c.pods.append(pod)
    return c


def _expected_pods(snap: Snapshot, pdb: PodDisruptionBudget, counts: _Counts) -> int:
    """Promised replicas.

    * Deployment/ReplicaSet pods promise the controller's ``spec.replicas``; a
      terminating pod's replacement is promised by the same controller, so the
      contribution is counted once regardless of in-flight disruption.
    * A bare (unmanaged) pod or a DaemonSet pod promises itself only **while
      alive**: once it is terminating there is no controller guaranteeing a
      replacement on the cordoned node, so it contributes 0.
    """
    total = 0
    seen_controller: set[str] = set()

    # Controller replica promises (live or terminating — counted once).
    for pod in list(counts.pods) + list(counts.terminating):
        if pod.terminal:
            continue
        w = resolve_workload(snap, pod)
        if w is None or w[0] == "DaemonSet":
            continue
        kind, obj = w
        wkey = f"{kind}:{pod.namespace}/{obj.name}"
        if wkey not in seen_controller:
            seen_controller.add(wkey)
            total += obj.replicas

    # Self-promising pods: bare pods and DaemonSet pods that are still alive.
    for pod in counts.pods:
        if pod.terminal:
            continue
        w = resolve_workload(snap, pod)
        if w is None or w[0] == "DaemonSet":
            total += 1
    return total


def _required_healthy(pdb: PodDisruptionBudget, expected: int) -> int:
    """Floor promised to the workload independent of in-flight disruption."""
    if pdb.min_available is not None:
        return _required_count(pdb.min_available, expected)
    assert pdb.max_unavailable is not None
    need = _required_count(pdb.max_unavailable, expected)
    return max(0, expected - need)


def account(
    snap: Snapshot,
    pdb: PodDisruptionBudget,
    extra_terminating: Optional[set[str]] = None,
) -> PdbAccounting:
    """Compute live PDB numbers, optionally pretending pods are terminating.

    Admission floors follow k8s ``disruption.go``:

    * ``minAvailable``: ``desiredHealthy = min(required, expected - disrupted)``
      (a disrupted slot lowers the admission target, but never below zero).
    * ``maxUnavailable``: ``desiredHealthy = expected - need - disrupted``
      (clamped) — k8s checks ``healthy + disrupted >= expected - need``.

    ``audit_floor`` is the budget promised to the running workload regardless
    of in-flight disruption; the audit asserts healthy never drops below it.
    """
    extra = extra_terminating or set()
    counts = _counts(snap, pdb, extra)
    live = [p for p in counts.pods if not p.terminal]
    expected = _expected_pods(snap, pdb, counts)
    healthy = sum(1 for p in live if p.ready and p.phase == "Running")
    # ``_counts`` already routes every deleted/hypothetical pod to
    # ``terminating``; count each exactly once.
    disrupted = len(counts.terminating)
    audit_floor = _required_healthy(pdb, expected)

    if pdb.min_available is not None:
        required = _required_count(pdb.min_available, expected)
        # minAvailable admits while healthy - in_wave >= required; a slot
        # already disrupted elsewhere lowers the same floor 1:1 (k8s uses
        # min(required, expected - disrupted)).
        desired_healthy = max(0, min(required, expected - disrupted))
    else:
        # maxUnavailable: at most ``need`` pods may be unavailable overall,
        # counting in-flight disruption. healthy must stay >= expected-need.
        need = _required_count(pdb.max_unavailable, expected)
        desired_healthy = max(0, expected - need)

    allowed = healthy - desired_healthy
    return PdbAccounting(
        key=pdb_key(pdb.namespace, pdb.name),
        expected=expected,
        healthy=healthy,
        disrupted=disrupted,
        desired_healthy=desired_healthy,
        audit_floor=audit_floor,
        disruptions_allowed=allowed,
    )


def matching_pdbs(snap: Snapshot, pod: Pod) -> list[PodDisruptionBudget]:
    return [p for p in snap.pdbs if pod_matches_pdb(pod, p)]


# --------------------------------------------------------------------------- #
# Admission (the multi-PDB intersection)
# --------------------------------------------------------------------------- #
@dataclass
class Rejection:
    pod: str
    namespace: str
    pdb: Optional[str]
    reason: str


def _candidate_healthy(pod: Pod) -> bool:
    return not pod.terminal and pod.ready and pod.phase == "Running"


def admit_evictions(
    snap: Snapshot, candidates: list[Pod]
) -> tuple[list[Pod], list[Rejection]]:
    """Greedily admit as many candidates as possible *in one wave*.

    A wave is atomic at admission time: every pod is checked against every
    PDB that selects it, and pods admitted earlier in the same wave consume
    budget immediately — that is the multi-PDB intersection.
    """
    admitted: list[Pod] = []
    rejected: list[Rejection] = []
    in_wave: set[str] = set()

    for pod in candidates:
        if pod.deleting:
            rejected.append(Rejection(pod.name, pod.namespace, None, "already terminating"))
            continue
        if pod.terminal:
            # Finished pods need no budget; the drain just removes them.
            admitted.append(pod)
            in_wave.add(pod.uid or pod.name)
            continue
        blocker = hard_eviction_blocker(pod)
        if blocker is not None:
            rejected.append(Rejection(pod.name, pod.namespace, None, blocker))
            continue

        pdbs = matching_pdbs(snap, pod)
        if not pdbs:
            admitted.append(pod)
            in_wave.add(pod.uid or pod.name)
            continue

        pod_ok = True
        fail_reason = ""
        fail_pdb: Optional[str] = None
        for pdb in pdbs:
            acct = account(snap, pdb, extra_terminating=in_wave | {pod.uid or pod.name})
            if _candidate_healthy(pod):
                # ``acct.healthy`` excludes every pod already admitted to this
                # wave and every terminating pod; compare it against the fixed
                # promised floor (auditFloor). That single comparison enforces
                # both minAvailable and maxUnavailable, including for pods
                # selected by multiple PDBs.
                if acct.healthy < acct.audit_floor:
                    pod_ok = False
                    fail_pdb = pdb_key(pdb.namespace, pdb.name)
                    fail_reason = (
                        f"pdb {fail_pdb}: eviction would leave "
                        f"{acct.healthy} healthy pods but the budget requires "
                        f"{acct.audit_floor} "
                        f"(expected={acct.expected}, disrupted={acct.disrupted})"
                    )
                    break
            else:
                # Unhealthy candidate.
                if pdb.unhealthy_policy != "AlwaysAllow":  # IfHealthyBudget
                    before = account(snap, pdb, extra_terminating=in_wave)
                    if before.healthy < before.audit_floor:
                        pod_ok = False
                        fail_pdb = pdb_key(pdb.namespace, pdb.name)
                        fail_reason = (
                            f"pdb {fail_pdb}: unhealthy pod not evictable under "
                            f"IfHealthyBudget (healthy={before.healthy} "
                            f"< required={before.audit_floor})"
                        )
                        break
                # AlwaysAllow: an unhealthy pod is always admitted, even while
                # the budget is otherwise exhausted (k8s 1.26+ semantics).
        if pod_ok:
            admitted.append(pod)
            in_wave.add(pod.uid or pod.name)
        else:
            rejected.append(Rejection(pod.name, pod.namespace, fail_pdb, fail_reason))
    return admitted, rejected


# --------------------------------------------------------------------------- #
# Audit — used after every simulator transition and by the acceptance tests
# --------------------------------------------------------------------------- #
@dataclass
class Violation:
    pdb: str
    detail: str


def audit_snapshot(snap: Snapshot) -> list[Violation]:
    """Return every currently-observed PDB safety violation.

    The invariant is the budget promised to the running workload: the number
    of ready, running pods must never drop below ``auditFloor`` while the
    workload still promises ``expected`` replicas. This is independent of
    in-flight disruption and is exactly what a correct drain must preserve.
    """
    out: list[Violation] = []
    for pdb in snap.pdbs:
        acct = account(snap, pdb)
        if acct.expected > 0 and acct.healthy < acct.audit_floor:
            out.append(
                Violation(
                    pdb_key(pdb.namespace, pdb.name),
                    f"healthy={acct.healthy} below auditFloor="
                    f"{acct.audit_floor} (expected={acct.expected}, "
                    f"disrupted={acct.disrupted})",
                )
            )
    return out
