"""Node drain planner.

Given a *local snapshot* and a set of nodes to drain, the planner produces an
explicitly ordered list of steps::

    Cordon(node) ...                       # one per target node, first
    EvictWave(0, [pods...])                # explicit batch
    EvictWave(1, [pods...])                # only after wave 0 fully settled
    ...
    Complete(nodes, uncordon)

Each wave is a set of pods simultaneously admissible **right now** against
*every* PDB selecting each pod (the multi-PDB intersection).  Waves are
serialised on purpose: wave ``n+1`` is only built after wave ``n`` has fully
settled — a deleted pod is never counted as its own replacement.

The initial plan is computed by shadow-running the whole drain to completion,
so clients can see the full predicted sequence up front.  At run time the
executor does not blindly replay those predicted pods: after every executed
wave it calls :func:"next_wave" against the *live* snapshot, which both
handles generation drift and supports the "replacement never becomes ready"
case (no further wave is produced until the world actually settles).

Pods that can never leave (mirror/static pods) or that have nowhere to be
rescheduled are reported as blockers.
"""
from __future__ import annotations

from dataclasses import dataclass

from .eviction import (
    admit_evictions,
    account,
    hard_eviction_blocker,
    is_daemonset_pod,
    pdb_key,
    resolve_workload,
)
from .models import (
    Blocker,
    CordonStep,
    CompleteStep,
    EvictWaveStep,
    Plan,
    Pod,
    Snapshot,
)
from .simulator import Simulator


@dataclass
class PlanRequest:
    drain_id: str
    nodes: list[str]
    ignore_daemon_sets: bool = True
    uncordon_on_complete: bool = False


def _pod_sort_key(pod: Pod):
    # Higher priority first, then namespace/name for full determinism.
    return (-pod.priority, pod.namespace, pod.name)


def drain_set(snap: Snapshot, nodes: list[str]) -> set[str]:
    return set(nodes)


def target_evictable_pods(
    snap: Snapshot, nodes: list[str], ignore_daemon_sets: bool
) -> tuple[list[Pod], list[Blocker]]:
    """Pods on the drain set that a drain may still evict, plus hard blockers."""
    node_set = drain_set(snap, nodes)
    out: list[Pod] = []
    blockers: list[Blocker] = []
    for pod in snap.pods:
        if pod.node not in node_set or pod.deleting:
            continue
        if ignore_daemon_sets and is_daemonset_pod(snap, pod):
            continue
        reason = hard_eviction_blocker(pod)
        if reason is not None:
            blockers.append(Blocker(
                node=pod.node, pod=pod.name, namespace=pod.namespace, reason=reason,
            ))
            continue
        out.append(pod)
    out.sort(key=_pod_sort_key)
    return out, blockers


def destination_nodes(snap: Snapshot, draining: set[str]) -> list[str]:
    return [
        n.name for n in snap.nodes
        if n.schedulable and n.name not in draining
    ]


def _capacity_blockers(snap: Snapshot, pods: list[Pod], draining: set[str]):
    if destination_nodes(snap, draining):
        return []
    managed = [
        p for p in pods
        if (w := resolve_workload(snap, p)) is not None and w[0] != "DaemonSet"
    ]
    return [
        Blocker(
            node=p.node, pod=p.name, namespace=p.namespace,
            reason="no schedulable node outside the drain set can host the "
                   "replacement pod",
        )
        for p in managed
    ]


def predicted_pdbs(snap: Snapshot) -> dict[str, int]:
    return {
        pdb_key(p.namespace, p.name): account(snap, p).disruptions_allowed
        for p in snap.pdbs
    }


def next_wave(
    snap: Snapshot,
    nodes: list[str],
    ignore_daemon_sets: bool,
    wave_index: int,
    already_evicted: set[str],
    *,
    include_waiting: bool = False,
) -> tuple[list[Pod], list[Blocker], list[str]]:
    """Build the next executable wave from a live snapshot.

    Returns ``(pods, blockers, notes)``. With ``include_waiting=False`` (the
    safe default used by the shadow planner) ``pods`` contains only pods
    admitted right now and is empty when nothing is admissible. With
    ``include_waiting=True`` (used by the executor to render a WAITING step),
    ``pods`` additionally contains candidates the PDBs reject right now, so a
    wave step can be shown whose preconditions explain what it is waiting for.
    """
    candidates, blockers = target_evictable_pods(snap, nodes, ignore_daemon_sets)
    notes: list[str] = []
    if blockers:
        notes.append("hard blocker(s) present")
    pending = [p for p in candidates if (p.uid or p.name) not in already_evicted]
    if not pending:
        return [], blockers, notes

    draining = set(nodes)
    cap = _capacity_blockers(snap, pending, draining)
    if cap:
        blockers += cap
        return [], blockers, notes

    admitted, rejected = admit_evictions(snap, pending)
    non_terminal = [p for p in admitted if not p.terminal]
    if non_terminal:
        return non_terminal, blockers, notes
    if rejected and include_waiting:
        notes.append(
            "PDBs admit none of "
            + ", ".join(r.pod for r in rejected[:5])
            + " right now; waiting for a disruption slot"
        )
        return pending, blockers, notes
    if rejected:
        notes.append(
            "PDBs admit none of "
            + ", ".join(r.pod for r in rejected[:5])
            + " right now; waiting for a disruption slot"
        )
    return [], blockers, notes


# --------------------------------------------------------------------------- #
# Full predicted plan via a shadow run to completion
# --------------------------------------------------------------------------- #
def build_plan(snap: Snapshot, req: PlanRequest) -> Plan:
    nodes_sorted = sorted(req.nodes)
    unknown = [n for n in nodes_sorted if not any(x.name == n for x in snap.nodes)]
    if unknown:
        raise ValueError(f"unknown nodes: {', '.join(unknown)}")

    steps: list = [CordonStep(node=n) for n in nodes_sorted]
    rationale: list[str] = [
        f"cordon {len(nodes_sorted)} node(s) first so the scheduler never "
        f"places replacements on a draining node"
    ]
    blockers: list[Blocker] = []

    initial, hard_blockers = target_evictable_pods(
        snap, nodes_sorted, req.ignore_daemon_sets
    )
    blockers.extend(hard_blockers)
    ds_count = len([
        p for p in snap.pods
        if p.node in set(nodes_sorted)
        and req.ignore_daemon_sets and is_daemonset_pod(snap, p)
    ])
    if ds_count:
        rationale.append(
            f"{ds_count} daemonset pod(s) skipped (ignoreDaemonSets=true); "
            f"they never block drain completion"
        )

    # Shadow-run the drain on a copy. The shadow is used only to *predict wave
    # structure* (which pods are simultaneously PDB-admissible and in what
    # order), so replacements mature instantly there. The real readiness wait
    # is enforced by the executor's precondition barriers against the live
    # simulator; a slow/stuck replacement therefore cannot corrupt planning.
    shadow = Simulator(snap, instant_ready=True)
    for n in nodes_sorted:
        shadow.cordon(n)

    evicted: set[str] = set()
    wave_index = 0
    while True:
        _settle(shadow)
        pods, wave_blockers, notes = next_wave(
            shadow.snap, nodes_sorted, req.ignore_daemon_sets, wave_index, evicted
        )
        for b in wave_blockers:
            if not any(x.pod == b.pod and x.reason == b.reason for x in blockers):
                blockers.append(b)
        if not pods:
            break
        steps.append(EvictWaveStep(
            index=wave_index,
            pods=[p.name for p in pods],
            predictedPdbs=predicted_pdbs(shadow.snap),
        ))
        rationale.append(
            f"wave {wave_index}: evict {len(pods)} pod(s) "
            f"({', '.join(p.name for p in pods[:4])}"
            f"{'...' if len(pods) > 4 else ''}); next wave waits for replacements"
        )
        for pod in pods:
            shadow.evict(pod, drain_id=req.drain_id)
            evicted.add(pod.uid or pod.name)
        _settle(shadow)
        wave_index += 1

    if blockers:
        rationale.append(
            f"{len(blockers)} blocker(s): mirror/static pods or unschedulable "
            f"replacements prevent an empty drain set"
        )

    steps.append(CompleteStep(nodes=nodes_sorted, uncordon=req.uncordon_on_complete))
    pdb_baseline = {
        pdb_key(p.namespace, p.name): account(snap, p).healthy
        for p in snap.pdbs
    }
    return Plan(
        drainId=req.drain_id,
        generation=snap.generation,
        nodes=nodes_sorted,
        ignoreDaemonSets=req.ignore_daemon_sets,
        steps=steps,
        blockers=blockers,
        pdbBaseline=pdb_baseline,
        rationale=rationale,
    )


def _settle(shadow: Simulator, max_ticks: int = 100) -> bool:
    """Advance a shadow simulator until this drain's in-flight work is done.

    Only waits for (a) terminating pods to be garbage-collected and (b)
    replacement pods the drain itself created to become Ready. Pods that were
    already NotReady in the input are not ours to heal. Returns True if the
    world settled, False if ``max_ticks`` elapsed (e.g. a replacement that the
    input makes perpetually Pending) — the predicted plan is then truncated
    and the executor enforces the real readiness barrier at run time.
    """
    for _ in range(max_ticks):
        pending = any(
            p.origin == "replacement" and (not p.ready or p.phase == "Pending")
            and not p.deleting
            for p in shadow.snap.pods
        )
        terminating = any(p.deleting for p in shadow.snap.pods)
        if not pending and not terminating:
            return True
        shadow.tick(1)
    return False
