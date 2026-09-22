"""
Service layer: state-changing domain operations shared by the REST API and
the scheduling engine.

Rules enforced here (independent of transport):
  * every job settlement is a conditional UPDATE -> at most one settler wins;
  * node GPU counters are only mutated while the node row is locked;
  * resource-pool GPU quota is enforced when a node (re)reports hardware;
  * audit rows are written inside the same transaction as the change.
"""
from django.conf import settings
from django.db import transaction

from .models import (
    Allocation,
    DecisionKind,
    FinishReason,
    Job,
    JobState,
    Node,
    NodeAdminState,
    ResourcePool,
    SchedulingDecision,
)


class SchedulerError(Exception):
    """Domain-level error mapped by views to HTTP 409."""


def sched_config():
    return settings.SCHEDULER


def log_decision(kind, summary, *, pool=None, job=None, node=None,
                 preemption=None, detail=None, using="default"):
    return SchedulingDecision.objects.using(using).create(
        kind=kind,
        summary=summary[:255],
        pool=pool,
        job=job,
        node=node,
        preemption=preemption,
        detail=detail or {},
    )


# ---------------------------------------------------------------------------
# Pools / nodes
# ---------------------------------------------------------------------------

def create_pool(name, max_total_gpus, max_concurrent_jobs, description=""):
    return ResourcePool.objects.create(
        name=name,
        max_total_gpus=max_total_gpus,
        max_concurrent_jobs=max_concurrent_jobs,
        description=description,
    )


def update_pool(pool, *, max_total_gpus=None, max_concurrent_jobs=None):
    """Shrinking a quota below currently-allocated GPUs is rejected."""
    with transaction.atomic():
        locked = ResourcePool.objects.select_for_update().get(pk=pool.pk)
        new_gpu_quota = (
            max_total_gpus if max_total_gpus is not None else locked.max_total_gpus
        )
        in_use = sum(
            n.allocated_gpus for n in locked.nodes.all()
        )
        if new_gpu_quota < in_use:
            raise SchedulerError(
                f"Cannot set max_total_gpus={new_gpu_quota}: "
                f"{in_use} GPUs are already allocated in pool '{locked.name}'"
            )
        locked.max_total_gpus = new_gpu_quota
        if max_concurrent_jobs is not None:
            locked.max_concurrent_jobs = max_concurrent_jobs
        locked.save()
        return locked


def register_node(pool, hostname, gpu_count, gpu_memory_mb):
    """Create or re-register a node with its hardware report.

    The node may not make the pool exceed its total-GPU quota.
    """
    if gpu_count <= 0:
        raise SchedulerError("gpu_count must be >= 1")
    if gpu_memory_mb <= 0:
        raise SchedulerError("gpu_memory_mb must be >= 1")

    with transaction.atomic():
        locked_pool = ResourcePool.objects.select_for_update().get(pk=pool.pk)
        existing = (
            Node.objects.select_for_update()
            .filter(hostname=hostname)
            .first()
        )
        other_gpus = sum(
            n.gpu_count for n in locked_pool.nodes.all()
            if existing is None or n.pk != existing.pk
        )
        if other_gpus + gpu_count > locked_pool.max_total_gpus:
            raise SchedulerError(
                f"Registering {gpu_count} GPUs on '{hostname}' would exceed pool "
                f"'{locked_pool.name}' quota "
                f"({other_gpus}+{gpu_count} > {locked_pool.max_total_gpus})"
            )

        if existing is not None:
            if existing.pool_id != locked_pool.pk:
                raise SchedulerError(
                    f"Node '{hostname}' already belongs to another pool"
                )
            # Hardware report changed; never shrink below live allocations.
            if gpu_count < existing.allocated_gpus:
                raise SchedulerError(
                    f"Cannot shrink '{hostname}' to {gpu_count} GPUs: "
                    f"{existing.allocated_gpus} are currently allocated"
                )
            existing.gpu_count = gpu_count
            existing.gpu_memory_mb = gpu_memory_mb
            existing.save(update_fields=["gpu_count", "gpu_memory_mb"])
            return existing

        return Node.objects.create(
            pool=locked_pool,
            hostname=hostname,
            gpu_count=gpu_count,
            gpu_memory_mb=gpu_memory_mb,
        )


def heartbeat(hostname, now, *, reported_gpus=None, reported_memory_mb=None):
    """Record a node heartbeat and perform local self-healing.

    * refreshes ``last_heartbeat_at``;
    * clears ``marked_offline_at`` when contact returns (heartbeat recovery),
      but never overrides an explicit administrative OFFLINE;
    * reconciles the in-memory allocation counter against the Allocation
      table (this is also how a restarted service re-syncs its counters).
    """
    with transaction.atomic():
        try:
            node = Node.objects.select_for_update().get(hostname=hostname)
        except Node.DoesNotExist:
            raise SchedulerError(f"Unknown node '{hostname}'; register it first")

        timeout = sched_config()["HEARTBEAT_TIMEOUT_SECONDS"]
        was_lost = node.is_heartbeat_stale(now, timeout) or bool(
            node.marked_offline_at
        )

        node.last_heartbeat_at = now
        if reported_gpus is not None and reported_gpus >= node.allocated_gpus:
            node.gpu_count = reported_gpus
        if reported_memory_mb is not None:
            node.gpu_memory_mb = reported_memory_mb

        # Self-heal the counter from the authoritative allocation rows.
        alive_total = node.alive_allocation_total()
        if node.allocated_gpus != alive_total:
            node.allocated_gpus = alive_total

        recovered = False
        # Heartbeat recovery only applies while the node is administratively
        # ONLINE. An operator-parked node stays offline until explicitly
        # re-enabled via set_admin_state.
        if (
            was_lost
            and node.marked_offline_at
            and node.admin_state == NodeAdminState.ONLINE
        ):
            node.marked_offline_at = None
            recovered = True

        node.save()

        if recovered:
            log_decision(
                DecisionKind.NODE_RECOVERED,
                f"Node '{node.hostname}' heartbeat recovered",
                pool=node.pool,
                node=node,
                detail={"allocated_gpus": node.allocated_gpus},
            )
        return node


def set_admin_state(node, admin_state, now):
    """Operator marks a node draining / online / offline."""
    with transaction.atomic():
        locked = Node.objects.select_for_update().get(pk=node.pk)
        locked.admin_state = admin_state

        if admin_state == NodeAdminState.OFFLINE:
            # Operator forced offline: behave exactly like a lost node so the
            # GPUs cannot be treated as available.
            locked.save(update_fields=["admin_state"])
            mark_node_lost(locked, now, cause="admin_offline")
            return locked

        if admin_state == NodeAdminState.ONLINE and locked.marked_offline_at:
            # Explicit re-enable after an administrative offline.
            locked.marked_offline_at = None
        locked.save(update_fields=["admin_state", "marked_offline_at"])
        return locked


def mark_node_lost(node, now, *, cause="heartbeat_timeout"):
    """Settle everything running on a node that can no longer be reached.

    Must be called with the node row locked. Counter is zeroed and every live
    job is settled exactly once (FAILED / NODE_LOST). A heartbeat-timeout loss
    does NOT touch ``admin_state`` (the operator still considers it online),
    so a later heartbeat can naturally recover it; an admin-offline loss parks
    the node until explicitly re-enabled.
    """
    if node.marked_offline_at:
        return  # already lost; idempotent
    node.marked_offline_at = now
    update_fields = ["marked_offline_at", "allocated_gpus"]
    if cause == "admin_offline":
        node.admin_state = NodeAdminState.OFFLINE
        update_fields.append("admin_state")
    node.allocated_gpus = 0
    node.save(update_fields=update_fields)

    # Fail in-flight preemption requests targeting this node.
    pending_reqs = list(
        node.incoming_preemptions.select_for_update().filter(status="PENDING")
    )

    jobs = list(
        Job.objects.select_for_update()
        .filter(allocation__node=node)
        .filter(state__in=[JobState.RUNNING, JobState.PREEMPTING])
        .order_by("id")
    )
    for job in jobs:
        _settle_locked(
            node,
            job,
            JobState.FAILED,
            FinishReason.NODE_LOST,
            now,
            extra_detail={"lost_node": node.hostname},
        )

    log_decision(
        DecisionKind.NODE_OFFLINE,
        f"Node '{node.hostname}' marked OFFLINE ({cause}); "
        f"{len(jobs)} live job(s) settled",
        pool=node.pool,
        node=node,
        detail={"cause": cause, "settled_jobs": [j.id for j in jobs]},
    )

    # Imported here to avoid a circular import (engine imports services).
    from .engine import fail_preemption_on_lost_node

    for req in pending_reqs:
        fail_preemption_on_lost_node(req, node, now)


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------

def create_job(pool, name, min_gpus, max_gpus, gpu_memory_mb, priority):
    return Job.objects.create(
        pool=pool,
        name=name,
        min_gpus=min_gpus,
        max_gpus=max_gpus,
        gpu_memory_mb=gpu_memory_mb,
        priority=priority,
    )


def settle_job(job, final_state, reason, now, *, expected_states=None,
               detail=None):
    """Compare-and-set a job to a terminal state and release its allocation.

    Returns (won: bool, job). ``won=False`` means another settler/transition
    got there first and this call changed nothing — this is what makes
    completion, cancellation, timeout and heartbeat-loss safe when they race.

    Lock order inside: pool row first, then node row, then job row — the same
    order the scheduling engine uses (pool -> nodes(sorted) -> jobs(sorted)).
    """
    with transaction.atomic():
        job = Job.objects.get(pk=job.pk)

        alloc = (
            Allocation.objects.select_related("node", "pool")
            .filter(job=job)
            .first()
        )
        if alloc is not None:
            # Lock pool before node to respect the global lock order.
            list(ResourcePool.objects.select_for_update().filter(pk=alloc.pool_id))
            list(Node.objects.select_for_update().filter(pk=alloc.node_id))

        qs = Job.objects.select_for_update().filter(pk=job.pk)
        if expected_states is not None:
            qs = qs.filter(state__in=list(expected_states))
        if job.state in (
            JobState.COMPLETED,
            JobState.CANCELLED,
            JobState.FAILED,
        ):
            return False, job

        updated = qs.update(
            state=final_state,
            finish_reason=reason,
            finished_at=now,
        )
        if not updated:
            return False, job

        job.refresh_from_db()

        if alloc is not None:
            node = alloc.node
            _release_allocation(node, alloc, now)

        log_decision(
            DecisionKind.SETTLED,
            f"Job #{job.id} -> {final_state} ({reason})",
            pool=job.pool,
            job=job,
            node=alloc.node if alloc else None,
            detail={"final_state": final_state, "reason": reason, **(detail or {})},
        )
        return True, job


def _settle_locked(node, job, final_state, reason, now, *, extra_detail=None):
    """Settle a job when pool/node/job rows are already locked by the caller.

    ``node`` may be None for a job that was never placed (e.g. a queued job
    cancelled by the queue timeout).
    """
    if job.state in (JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED):
        return False
    job.state = final_state
    job.finish_reason = reason
    job.finished_at = now
    job.save(update_fields=["state", "finish_reason", "finished_at"])

    alloc = Allocation.objects.filter(job=job).first()
    if alloc is not None:
        _release_allocation(node, alloc, now)

    log_decision(
        DecisionKind.SETTLED,
        f"Job #{job.id} -> {final_state} ({reason})",
        pool=job.pool,
        job=job,
        node=node,
        detail={"final_state": final_state, "reason": reason, **(extra_detail or {})},
    )
    return True


def _release_allocation(node, alloc, now):
    """Delete an allocation row and give GPUs back to the node.

    Called only by a transaction that has already won the job state
    transition, so the counter is decremented exactly once per allocation.

    The decrement is an atomic conditional UPDATE (``Greatest(0, ...)``) on
    the already-locked node row. We deliberately do NOT ``refresh_from_db``
    the caller's node instance here: inside a transaction that would pull a
    snapshot that may precede this same transaction's earlier increment, and
    saving it would silently overwrite the decrement.
    """
    from django.db.models import Case, F, Value, When
    from django.db.models import IntegerField

    gpus = alloc.gpu_count
    node_id = alloc.node_id
    alloc.delete()
    # Conditional decrement. A plain ``Greatest(col - gpus, 0)`` is NOT safe
    # on MySQL: allocated_gpus is an UNSIGNED integer, so the subtraction is
    # evaluated before the clamp and raises BIGINT UNSIGNED out of range when
    # the intermediate result is negative. Branch in SQL instead.
    Node.objects.filter(pk=node_id).update(
        allocated_gpus=Case(
            When(allocated_gpus__gte=gpus, then=F("allocated_gpus") - gpus),
            default=Value(0),
            output_field=IntegerField(),
        )
    )
    # Keep any in-memory instance the caller holds roughly consistent; the
    # database row remains authoritative.
    if node is not None and getattr(node, "pk", None) == node_id:
        node.allocated_gpus = max(0, node.allocated_gpus - gpus)


def cancel_job(job, now):
    """User cancellation. Works for queued and running jobs."""
    return settle_job(
        job,
        JobState.CANCELLED,
        FinishReason.USER_CANCELLED,
        now,
        expected_states=[
            JobState.PENDING,
            JobState.RUNNING,
            JobState.PREEMPTING,
        ],
    )


def report_completion(job, now):
    """Worker reports a normal finish. A worker completing at the same moment
    it is being preempted legitimately wins as COMPLETED."""
    return settle_job(
        job,
        JobState.COMPLETED,
        FinishReason.COMPLETED,
        now,
        expected_states=[JobState.RUNNING, JobState.PREEMPTING],
    )


def report_preempted_release(job, now):
    """Worker acknowledges eviction and has released its GPUs."""
    return settle_job(
        job,
        JobState.CANCELLED,
        FinishReason.PREEMPTED,
        now,
        expected_states=[JobState.PREEMPTING],
        detail={"released_by": "worker"},
    )


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

def recover_from_database(now):
    """Rebuild allocation counters from the Allocation table.

    All scheduling state lives in MySQL, so after a process restart the
    job<->node assignments are reconstructed from surviving allocation rows.
    Nodes that never heartbeated again will be handled by the normal
    lost-node path on the next tick.
    """
    recovered = 0
    with transaction.atomic():
        nodes = list(Node.objects.select_for_update().order_by("id"))
        for node in nodes:
            alive_total = node.alive_allocation_total()
            if node.allocated_gpus != alive_total:
                node.allocated_gpus = alive_total
                node.save(update_fields=["allocated_gpus"])
                recovered += 1
    return {"nodes_reconciled": recovered, "nodes_total": len(nodes)}
