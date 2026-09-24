"""In-memory Kubernetes-like cluster simulator.

The executor is only ever allowed to touch this object — there is no real
apiserver.  A simulation owns a mutable :class:`Snapshot`, a monotonically
increasing ``generation`` (bumped on every mutation, including readiness flips)
and a logical ``tick`` clock.  Pods created as replacements start Pending and
become ready after their Deployment's ``readyDelay`` ticks, which lets the
test-suite drive the "replacement pod never becomes ready" scenario.
"""
from __future__ import annotations

import itertools
from typing import Optional

from .eviction import audit_snapshot, resolve_workload
from .models import Node, Pod, Snapshot


class SimError(Exception):
    pass


class AuditError(SimError):
    """Raised if a simulator transition would produce a PDB violation."""


_uid_counter = itertools.count(1)


def _uid() -> str:
    return f"sim-{next(_uid_counter):06d}"


class Simulator:
    def __init__(self, snapshot: Snapshot, instant_ready: bool = False):
        snap = snapshot.model_copy(deep=True)
        if snap.generation == 0:
            snap.generation = 1
        for pod in snap.pods:
            if not pod.uid:
                pod.uid = _uid()
        self.snap = snap
        # Planning-only mode: replacements are born Ready so the shadow run
        # can predict the whole wave structure without real readiness waits.
        self.instant_ready = instant_ready
        self.events: list[dict] = []
        self._drained_nodes: set[str] = set()
        # Highest generation produced by an explicit out-of-band mutation.
        self.external_generation = snap.generation
        self._log("init", f"snapshot loaded at generation {snap.generation}")

    # ------------------------------------------------------------------ #
    def _log(self, type_: str, detail: str) -> None:
        self.events.append(
            {"tick": self.snap.tick, "generation": self.snap.generation,
             "type": type_, "detail": detail}
        )

    def _bump(self) -> None:
        self.snap.generation += 1

    def _external_bump(self) -> None:
        """Bump generation and raise the out-of-band watermark."""
        self._bump()
        self.external_generation = self.snap.generation

    def _audit(self, where: str) -> None:
        violations = audit_snapshot(self.snap)
        if violations:
            msgs = "; ".join(f"{v.pdb}: {v.detail}" for v in violations)
            raise AuditError(f"post-condition violated after {where}: {msgs}")

    @property
    def generation(self) -> int:
        return self.snap.generation

    def get_pod(self, namespace: str, name: str) -> Pod:
        for pod in self.snap.pods:
            if pod.namespace == namespace and pod.name == name:
                return pod
        raise SimError(f"pod {namespace}/{name} not found")

    # ------------------------------------------------------------------ #
    def mark_draining(self, node: str) -> None:
        """Register a node as actively draining: replacements avoid it."""
        self._drained_nodes.add(node)

    def _candidate_nodes(self) -> list[Node]:
        nodes = [
            n for n in self.snap.nodes
            if n.schedulable and n.name not in self._drained_nodes
        ]
        if not nodes:
            raise SimError("no schedulable node outside the drain set can host replacements")
        return nodes

    def _schedule(self) -> Node:
        nodes = self._candidate_nodes()
        counts = {n.name: 0 for n in nodes}
        for pod in self.snap.pods:
            if not pod.deleting and pod.node in counts:
                counts[pod.node] += 1
        # Fewest pods wins; node name is the deterministic tie-break.
        return min(nodes, key=lambda n: (counts[n.name], n.name))

    # ------------------------------------------------------------------ #
    def cordon(self, node_name: str) -> None:
        node = self.node(node_name)
        if not node.schedulable:
            return
        node.schedulable = False
        self.mark_draining(node_name)
        self._bump()
        self._log("cordon", f"node {node_name} marked unschedulable")

    def uncordon(self, node_name: str) -> None:
        node = self.node(node_name)
        if node.schedulable:
            return
        node.schedulable = True
        self._drained_nodes.discard(node_name)
        self._bump()
        self._log("uncordon", f"node {node_name} schedulable again")

    def node(self, name: str) -> Node:
        for n in self.snap.nodes:
            if n.name == name:
                return n
        raise SimError(f"node {name} not found")

    # ------------------------------------------------------------------ #
    def evict(self, pod: Pod, drain_id: str) -> None:
        """Admit one eviction: set deletionTimestamp and create a replacement.

        This is invoked only for pods that already passed PDB admission. The
        transition is audited immediately — if our accounting is ever wrong
        the simulator refuses the state change rather than hiding it.
        """
        live = self.get_pod(pod.namespace, pod.name)
        if live.deleting:
            raise SimError(f"pod {pod.namespace}/{pod.name} already terminating")

        workload = resolve_workload(self.snap, live)
        if workload is not None and workload[0] == "DaemonSet":
            raise SimError(f"daemonset pod {live.name} must not be evicted")

        live.deletion_tick = self.snap.tick
        self._bump()
        self._log("evict", f"pod {live.namespace}/{live.name} evicted by {drain_id}")

        if workload is None:
            # Bare pod: nothing promises a replacement.
            self._audit(f"eviction of {live.name}")
            return

        kind, obj = workload
        if kind == "Deployment":
            delay = obj.ready_delay
            rs_hint = f"{obj.name}-"
            owner_kind, owner_name = "ReplicaSet", f"{obj.name}-simrs"
        else:
            delay = 0
            rs_hint = f"{obj.name}-"
            owner_kind, owner_name = "ReplicaSet", obj.name

        node = self._schedule()
        ready_now = self.instant_ready
        replacement = Pod(
            name=f"{rs_hint}r{self.snap.generation:04x}-{_uid()}",
            namespace=live.namespace,
            node=node.name,
            phase="Running" if ready_now else "Pending",
            ready=ready_now,
            ready_at=None if ready_now else self.snap.tick + max(1, delay),
            labels=dict(live.labels),
            owner={"kind": owner_kind, "name": owner_name, "uid": _uid()},
            origin="replacement",
            uid=_uid(),
        )
        self.snap.pods.append(replacement)
        self._bump()
        if ready_now:
            # Planning shadow: the old pod is GCed immediately so healthy
            # accounting reflects the completed rotation.
            live.deletion_tick = self.snap.tick - 1
            self.snap.pods.remove(live)
            self._bump()
        self._log(
            "replace",
            f"replacement {replacement.namespace}/{replacement.name} scheduled on "
            f"{node.name}; "
            + ("ready immediately (planning shadow)" if ready_now
               else f"readyAt tick {replacement.ready_at}"),
        )
        self._audit(f"eviction of {live.name}")

    # ------------------------------------------------------------------ #
    def tick(self, steps: int = 1) -> bool:
        """Advance the simulated clock; return whether anything changed."""
        changed = False
        for _ in range(max(1, steps)):
            self.snap.tick += 1
            t = self.snap.tick
            # Garbage-collect terminating pods (grace period: 1 tick).
            for pod in [p for p in self.snap.pods if p.deleting and t > p.deletion_tick]:
                self.snap.pods.remove(pod)
                changed = True
                self._log("gc", f"terminating pod {pod.namespace}/{pod.name} removed")
            # Mature replacements / waiting pods.
            for pod in self.snap.pods:
                if not pod.deleting and pod.ready_at is not None and t >= pod.ready_at:
                    if pod.phase != "Running" or not pod.ready:
                        pod.phase = "Running"
                        pod.ready = True
                        pod.ready_at = None
                        changed = True
                        self._log("ready", f"pod {pod.namespace}/{pod.name} became ready")
        if changed:
            self._bump()
            self._audit("tick")
        return changed

    # ------------------------------------------------------------------ #
    # External ("out of band") mutations used to test re-planning
    # ------------------------------------------------------------------ #
    def external_set_pod_ready(
        self, namespace: str, name: str, ready: bool
    ) -> None:
        pod = self.get_pod(namespace, name)
        if ready:
            pod.ready = True
            pod.phase = "Running"
            pod.ready_at = None
        else:
            pod.ready = False
            pod.phase = "Running"
            pod.ready_at = None
        self._external_bump()
        self._log("external", f"pod {namespace}/{name} ready={ready} set out of band")
        self._audit("external mutation")

    def external_add_pod(self, pod: dict, ready_delay: Optional[int] = None) -> Pod:
        new_pod = Pod(**pod)
        if not new_pod.uid:
            new_pod.uid = _uid()
        if new_pod.origin == "original":
            new_pod.origin = "external"
        if ready_delay is not None and not new_pod.ready:
            new_pod.ready_at = self.snap.tick + ready_delay
        self.snap.pods.append(new_pod)
        self._external_bump()
        self._log("external", f"pod {new_pod.namespace}/{new_pod.name} added out of band")
        self._audit("external mutation")
        return new_pod

    def external_remove_pod(self, namespace: str, name: str) -> None:
        """Remove a pod out of band (e.g. an admin deleted a static manifest)."""
        self.get_pod(namespace, name)  # raises 404-style error if absent
        self.snap.pods = [
            p for p in self.snap.pods
            if not (p.namespace == namespace and p.name == name)
        ]
        self._external_bump()
        self._log("external", f"pod {namespace}/{name} removed out of band")
        self._audit("external mutation")

    def public_snapshot(self) -> Snapshot:
        """Deep copy safe to hand to planner / API clients."""
        return self.snap.model_copy(deep=True)
