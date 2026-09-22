"""
Scheduling engine.

One ``tick(now)`` performs, *per resource pool inside a single transaction*,
four phases in a fixed order:

    A. node liveness     - lost heartbeats -> OFFLINE, live jobs settled once
    B. queue timeouts    - PENDING jobs older than QUEUE_TIMEOUT_SECONDS cancelled
    C. preemption follow - wait for victims to release, force after grace,
                           fail when the target node was lost
    D. placement         - best-fit bin packing, then preemption (p>=8 vs <=3)

Concurrency
-----------
All work for a pool happens while that pool's row is locked
(``SELECT ... FOR UPDATE``), so multiple scheduler processes ticking at once
are serialized per pool and can never oversell GPUs, double-allocate a job,
or exceed pool quotas. Cross-pool ticks remain fully concurrent.

Global lock order everywhere in the codebase is:
    resource_pool -> nodes (id ASC) -> jobs (id ASC)
which prevents deadlocks against API-path transactions (complete / cancel /
heartbeat / admin-state) that follow the same order.

Sorting
-------
Pending jobs are ordered by  priority DESC, enqueued_at ASC, id ASC
(urgency first, then FIFO within one priority). Placement uses best-fit
bin packing (tightest residual GPU count) to reduce fragmentation; node id
is the deterministic tie-break.
"""
from django.db import transaction

from .models import (
    Allocation,
    DecisionKind,
    FinishReason,
    Job,
    JobState,
    Node,
    PreemptionRequest,
    PreemptionStatus,
    PreemptionVictim,
    ResourcePool,
    SchedulingDecision,
)
from .services import (
    _release_allocation,
    _settle_locked,
    log_decision,
    mark_node_lost,
    sched_config,
)


# ---------------------------------------------------------------------------
# Preemption helpers
# ---------------------------------------------------------------------------

def fail_preemption_on_lost_node(req, node, now):
    """Fail an in-flight preemption whose target node disappeared.

    Runs inside ``mark_node_lost`` (node + pool rows already locked). The
    victims have already been settled as FAILED/NODE_LOST; the beneficiary
    remains PENDING and will simply be scheduled elsewhere later — that is
    the failure recovery path.
    """
    req.status = PreemptionStatus.FAILED
    req.fail_reason = "target_node_lost"
    req.resolved_at = now
    req.save(update_fields=["status", "fail_reason", "resolved_at"])
    log_decision(
        DecisionKind.PREEMPT_FAILED,
        f"Preemption #{req.id} failed: target node '{node.hostname}' was lost",
        pool=req.pool,
        job=req.beneficiary,
        node=node,
        preemption=req,
        detail={"fail_reason": "target_node_lost"},
    )


def _finish_preemption(req, now, *, forced_victims, fail_reason=""):
    """Mark a request RELEASED (victims confirmed gone) or FAILED."""
    if fail_reason:
        req.status = PreemptionStatus.FAILED
        req.fail_reason = fail_reason
        kind = DecisionKind.PREEMPT_FAILED
        summary = f"Preemption #{req.id} failed: {fail_reason}"
    else:
        req.status = PreemptionStatus.RELEASED
        kind = DecisionKind.PREEMPT_RELEASED
        summary = (
            f"Preemption #{req.id}: {len(forced_victims)} victim(s) released; "
            f"job #{req.beneficiary_id} may be placed"
        )
    req.resolved_at = now
    req.save(update_fields=["status", "fail_reason", "resolved_at"])
    log_decision(
        kind,
        summary,
        pool=req.pool,
        job=req.beneficiary,
        node=req.target_node,
        preemption=req,
        detail={
            "forced": forced_victims,
            "fail_reason": fail_reason,
        },
    )


# ---------------------------------------------------------------------------
# Tick phases
# ---------------------------------------------------------------------------

def _phase_node_liveness(pool, now, timeout, stats):
    nodes = list(Node.objects.select_for_update().filter(pool=pool).order_by("id"))
    for node in nodes:
        # Heartbeat-timeout loss only (admin offline is handled by
        # set_admin_state and is marked_offline_at-idempotent).
        if node.is_heartbeat_stale(now, timeout) and not node.marked_offline_at:
            mark_node_lost(node, now, cause="heartbeat_timeout")
            stats["lost_nodes"] += 1


def _phase_queue_timeouts(pool, now, queue_timeout, stats):
    """Cancel PENDING jobs that have queued longer than the timeout (2h)."""
    pending = list(
        Job.objects.select_for_update()
        .filter(pool=pool, state=JobState.PENDING)
        .order_by("id")
    )
    for job in pending:
        age = (now - job.enqueued_at).total_seconds()
        if age > queue_timeout:
            # Queued job: no allocation/node yet, so node=None is valid.
            won = _settle_locked(
                None,
                job,
                JobState.CANCELLED,
                FinishReason.TIMEOUT,
                now,
                extra_detail={"age_seconds": age},
            )
            if won:
                stats["timeouts"] += 1


def _phase_preemption_followup(pool, now, grace, stats):
    """Confirm victims have actually released GPUs before re-allocating.

    A request is only considered RELEASED once every victim's allocation row
    is gone (worker notified release, or the grace window elapsed and the
    platform force-settles the victim). The beneficiary is never placed by
    this phase itself — phase D of a subsequent tick picks it up once the
    RELEASED request exists, so "release first, re-allocate after" is a hard
    ordering guarantee even across restarts.
    """
    reqs = list(
        PreemptionRequest.objects.select_for_update()
        .filter(pool=pool, status=PreemptionStatus.PENDING)
        .order_by("id")
    )
    for req in reqs:
        beneficiary = Job.objects.filter(pk=req.beneficiary_id).first()
        beneficiary_gone = (
            beneficiary is None or beneficiary.state != JobState.PENDING
        )
        node = Node.objects.select_for_update().filter(pk=req.target_node_id).first()

        # Target node vanished: its lost-node path fails in-flight requests;
        # guard here as well.
        if node is None or node.marked_offline_at:
            _finish_preemption(
                req, now, forced_victims=[], fail_reason="target_node_lost"
            )
            stats["preemption_failures"] += 1
            continue

        victim_rows = list(req.victim_rows.select_related("job").order_by("job_id"))
        still_occupying = []
        for vr in victim_rows:
            vjob = Job.objects.select_for_update().get(pk=vr.job_id)
            alloc = Allocation.objects.filter(job=vjob, node=node).first()
            if alloc is None:
                continue  # worker already released (or completed) -> GPUs back
            if vjob.state not in (JobState.PREEMPTING, JobState.RUNNING):
                # Job settled (terminal) but its allocation lingered: reclaim.
                _release_allocation(node, alloc, now)
                continue
            waited = (now - req.created_at).total_seconds()
            # A withdrawn beneficiary still gets its victims released (grace
            # applies), but never placed.
            if waited >= grace:
                _settle_locked(
                    node,
                    vjob,
                    JobState.CANCELLED,
                    FinishReason.PREEMPTED,
                    now,
                    extra_detail={
                        "forced": True,
                        "preemption_id": req.id,
                        "beneficiary_gone": beneficiary_gone,
                    },
                )
                stats["forced_preemptions"] += 1
            else:
                still_occupying.append(vjob.id)

        if still_occupying:
            continue  # real workers still hold the GPUs; wait before re-alloc

        if beneficiary_gone:
            _finish_preemption(
                req,
                now,
                forced_victims=[vr.job_id for vr in victim_rows],
                fail_reason="beneficiary_not_pending",
            )
            stats["preemption_failures"] += 1
        else:
            _finish_preemption(
                req, now, forced_victims=[vr.job_id for vr in victim_rows]
            )
            stats["preemptions_released"] += 1


def _pack(node, job):
    """Best-fit candidate: allocate as many as possible up to max_gpus.

    Returns gpu_count to allocate, or None when the node can't fit the
    minimum. Residual free GPUs drive the best-fit choice.
    """
    if node.gpu_memory_mb < job.gpu_memory_mb:
        return None
    if node.free_gpus < job.min_gpus:
        return None
    g = min(job.max_gpus, node.free_gpus)
    if g < job.min_gpus:
        return None
    return g


def _choose_node(nodes, job):
    """Tightest-fit (least residual free GPUs), tie-break node id ASC."""
    best = None  # (residual, node.id, node, gpu_count)
    examined = []
    for node in nodes:
        g = _pack(node, job)
        examined.append(
            {
                "node": node.hostname,
                "free_gpus": node.free_gpus,
                "gpu_memory_mb": node.gpu_memory_mb,
                "fit": g,
            }
        )
        if g is None:
            continue
        residual = node.free_gpus - g
        key = (residual, node.id)
        if best is None or key < (best[0], best[1]):
            best = (residual, node.id, node, g)
    return best, examined


def _preemption_plan(nodes, job, busy_victim_ids, now, timeout):
    """Find the minimum-disruption set of low-priority victims.

    Only nodes that currently accept new jobs (heartbeating, online, not
    draining, not lost) are candidates — a lost node's capacity must never be
    treated as available, and preemption can't resurrect it. Only RUNNING
    jobs with priority <= 3 are evictable; greedy by GPU count DESC (fewer
    victims), then priority ASC, id ASC. Prefer the plan with the fewest
    victims, then fewest evicted GPUs, then lowest node id.
    """
    best_plan = None  # (num_victims, evicted_gpus, node.id, node, victims)
    for node in nodes:
        if not node.accepts_new_jobs(now, timeout):
            continue
        if node.gpu_memory_mb < job.gpu_memory_mb or node.gpu_count < job.min_gpus:
            continue
        victims = list(
            Job.objects.filter(
                allocation__node=node,
                state=JobState.RUNNING,
                priority__lte=3,
            )
            .exclude(id__in=busy_victim_ids)
            .order_by("-allocation__gpu_count", "priority", "id")
        )
        # Annotate gpu counts from their allocations.
        victim_gpus = {
            j.id: Allocation.objects.get(job=j, node=node).gpu_count for j in victims
        }
        free = node.free_gpus
        chosen = []
        gained = 0
        for v in victims:
            if free + gained >= job.min_gpus:
                break
            chosen.append(v)
            gained += victim_gpus[v.id]
        if free + gained < job.min_gpus:
            continue
        # A plan that evicts nobody is meaningless: if the node already fits,
        # normal best-fit placement would have handled it. Require >=1 victim.
        if not chosen:
            continue
        evicted_gpus = sum(victim_gpus[v.id] for v in chosen)
        key = (len(chosen), evicted_gpus, node.id)
        if best_plan is None or key < (best_plan[0], best_plan[1], best_plan[2]):
            best_plan = (len(chosen), evicted_gpus, node.id, node, chosen, victim_gpus)
    return best_plan


def _place(pool, node, job, gpu_count, now, pinned_id=None, free_after=None):
    """Create an allocation. Caller holds the pool lock and candidate locks."""
    # Conditional claim: even within the pool serialization this makes a
    # double-allocation impossible if the job's row changed unexpectedly.
    claimed = Job.objects.filter(pk=job.pk, state=JobState.PENDING).update(
        state=JobState.RUNNING, scheduled_at=now
    )
    if not claimed:
        return False
    job.refresh_from_db()
    # Atomic SQL increment on the locked node row. Never assign+save a Python
    # snapshot here: this transaction may already have seen an earlier value
    # (e.g. a completion in another transaction), and saving it would
    # overwrite that decrement and oversell the node.
    from django.db.models import F

    Node.objects.filter(pk=node.pk).update(allocated_gpus=F("allocated_gpus") + gpu_count)
    Allocation.objects.create(
        job=job, node=node, pool=pool, gpu_count=gpu_count
    )
    preemption = None
    if pinned_id is not None:
        preemption = PreemptionRequest.objects.filter(pk=pinned_id).first()
    log_decision(
        DecisionKind.PLACED,
        f"Job #{job.id} ({job.name}, p={job.priority}) placed on "
        f"'{node.hostname}' with {gpu_count} GPU(s)",
        pool=pool,
        job=job,
        node=node,
        preemption=preemption,
        detail={
            "node": node.hostname,
            "gpu_count": gpu_count,
            "requested": {
                "min_gpus": job.min_gpus,
                "max_gpus": job.max_gpus,
                "gpu_memory_mb": job.gpu_memory_mb,
            },
            "basis": "best_fit_bin_packing|priority_desc_fifo",
            "node_free_after": free_after,
            "satisfied_released_preemption": pinned_id,
        },
    )
    return True


def _fresh_usage(pool):
    """{node_id: gpus_in_use} for the pool, authoritative within a tick.

    Usage is computed live from Allocation rows (the source of truth).
    Placements made earlier in the SAME tick have already inserted their
    allocation rows, so they are visible to this query and need no separate
    delta. We deliberately do NOT read ``Node.allocated_gpus`` here: inside a
    long transaction its snapshot can lag a completion that committed in
    another transaction (MySQL's FOR UPDATE blocks until that commit; SQLite's
    snapshot simply lags).
    """
    from django.db.models import Sum

    rows = (
        Allocation.objects.filter(node__pool=pool)
        .values("node_id")
        .annotate(total=Sum("gpu_count"))
        .values_list("node_id", "total")
    )
    return {nid: (total or 0) for nid, total in rows}


def _reconcile_node_counters(pool):
    """Resync each pool node's cached counter from the Allocation table.

    Runs at the start of phase D while all pool rows are locked. Completions
    and cancellations can commit in other transactions between ticks; on a
    snapshot-isolating database the cached counter (and even a raw F()
    increment's read) may momentarily lag those commits. The Allocation rows
    are authoritative, and after acquiring the pool lock we re-derive every
    counter from them so placement decisions and the persisted cache cannot
    drift (defensive; on MySQL FOR UPDATE this already reads the latest
    committed value, and the reconcile is then a harmless no-op).
    """
    live = _fresh_usage(pool)
    for node in Node.objects.filter(pool=pool):
        correct = 0 if node.marked_offline_at else live.get(node.id, 0)
        if node.allocated_gpus != correct:
            node.allocated_gpus = correct
            node.save(update_fields=["allocated_gpus"])


def _phase_place(pool, now, timeout, stats):
    # Defensive counter reconciliation before any decision (see docstring).
    _reconcile_node_counters(pool)

    # Beneficiaries of an in-flight preemption wait (they are placed only on a
    # later tick, after release is confirmed in phase C).
    waiting_ids = set(
        PreemptionRequest.objects.filter(
            pool=pool, status=PreemptionStatus.PENDING
        ).values_list("beneficiary_id", flat=True)
    )
    # A beneficiary whose request failed THIS tick (e.g. its target node was
    # lost) must not immediately spin up another request for the same lost
    # node in the same round; it retries on a later tick / another node.
    failed_this_tick_ids = set(
        SchedulingDecision.objects.filter(
            pool=pool,
            kind=DecisionKind.PREEMPT_FAILED,
            created_at=now,
        ).values_list("job_id", flat=True)
    )
    # A released request pins the beneficiary to the node whose victims were
    # actually evicted, so freed GPUs are used for the job that earned them.
    released_targets = dict(
        PreemptionRequest.objects.filter(
            pool=pool, status=PreemptionStatus.RELEASED
        ).values_list("beneficiary_id", "target_node_id")
    )
    # Victims already promised to another in-flight request can't be reused.
    busy_victim_ids = set(
        PreemptionVictim.objects.filter(
            request__pool=pool, request__status=PreemptionStatus.PENDING
        ).values_list("job_id", flat=True)
    )

    # Lock every pending job in id order first (stable lock order), then sort
    # by the scheduling policy in Python.
    pending = list(
        Job.objects.select_for_update()
        .filter(pool=pool, state=JobState.PENDING)
        .order_by("id")
    )
    pending = [
        j
        for j in pending
        if j.id not in waiting_ids and j.id not in failed_this_tick_ids
    ]
    pending.sort(key=lambda j: (-j.priority, j.enqueued_at, j.id))

    running_count = Job.objects.filter(
        pool=pool, state__in=[JobState.RUNNING, JobState.PREEMPTING]
    ).count()

    # Lock all pool node rows up front once, ordered by id (stable order).
    locked_nodes = {
        n.id: n
        for n in Node.objects.select_for_update().filter(pool=pool).order_by("id")
    }

    for job in pending:
        if running_count >= pool.max_concurrent_jobs:
            log_decision(
                DecisionKind.NO_FIT,
                f"Job #{job.id} waits: pool concurrent job quota reached "
                f"({running_count}/{pool.max_concurrent_jobs})",
                pool=pool,
                job=job,
                detail={"reason": "pool_concurrent_quota", "running": running_count},
            )
            continue

        # Fresh, allocation-derived usage for this iteration.
        usage = _fresh_usage(pool)

        class _View:
            """Lightweight node view carrying fresh usage for packing."""

            def __init__(self, node, used):
                self.id = node.id
                self.hostname = node.hostname
                self.gpu_count = node.gpu_count
                self.gpu_memory_mb = node.gpu_memory_mb
                self._used = used
                self.allocated_gpus = used

            @property
            def free_gpus(self):
                return self.gpu_count - self._used

        nodes = []
        for nid, node in locked_nodes.items():
            view = _View(node, usage.get(nid, 0))
            if node.accepts_new_jobs(now, timeout):
                nodes.append(view)

        # A beneficiary whose victims are confirmed released goes to the
        # target node first (that is where the freed GPUs are).
        pinned_id = released_targets.get(job.id)
        best = None
        examined = []
        if pinned_id is not None:
            pinned = next((n for n in nodes if n.id == pinned_id), None)
            if pinned is not None:
                g = _pack(pinned, job)
                examined.append(
                    {
                        "node": pinned.hostname,
                        "free_gpus": pinned.free_gpus,
                        "gpu_memory_mb": pinned.gpu_memory_mb,
                        "fit": g,
                        "pinned_by_released_preemption": True,
                    }
                )
                if g is not None:
                    best = (pinned.free_gpus - g, pinned.id, pinned, g)
        if best is None:
            best, examined_general = _choose_node(nodes, job)
            examined.extend(examined_general)

        if best is not None:
            _, _, view, gpu_count = best
            pool_allocated = sum(usage.values())
            if pool_allocated + gpu_count > pool.max_total_gpus:
                log_decision(
                    DecisionKind.NO_FIT,
                    f"Job #{job.id} waits: pool GPU quota reached",
                    pool=pool,
                    job=job,
                    detail={
                        "reason": "pool_gpu_quota",
                        "pool_allocated": pool_allocated,
                        "wanted": gpu_count,
                        "quota": pool.max_total_gpus,
                    },
                )
                continue
            real_node = locked_nodes[view.id]
            if _place(
                pool,
                real_node,
                job,
                gpu_count,
                now,
                pinned_id=pinned_id,
                free_after=view.free_gpus - gpu_count,
            ):
                running_count += 1
                stats["placements"] += 1
                continue

        # Cannot fit now. High-priority (>=8) jobs may preempt p<=3 jobs.
        if job.priority >= 8:
            # Preemption planning queries victims on the real locked node
            # models; give each its fresh, allocation-derived usage.
            for nid, node in locked_nodes.items():
                node.allocated_gpus = usage.get(nid, 0)
            plan = _preemption_plan(
                list(locked_nodes.values()),
                job,
                busy_victim_ids,
                now,
                timeout,
            )
            if plan is not None:
                _num, _gpus, _nid, node, victims, victim_gpus = plan
                _start_preemption(pool, node, job, victims, victim_gpus, now)
                stats["preemptions_started"] += 1
                continue
            log_decision(
                DecisionKind.NO_FIT,
                f"Job #{job.id} (p={job.priority}) could not fit and found no "
                f"preemptable low-priority (p<=3) jobs",
                pool=pool,
                job=job,
                detail={
                    "reason": "no_fit_and_no_preemptable_victims",
                    "nodes_examined": examined,
                },
            )
        else:
            log_decision(
                DecisionKind.NO_FIT,
                f"Job #{job.id} (p={job.priority}) could not fit any node",
                pool=pool,
                job=job,
                detail={"reason": "no_fit", "nodes_examined": examined},
            )


def _start_preemption(pool, node, beneficiary, victims, victim_gpus, now):
    """Phase 1 of preemption: mark victims, record basis, release nothing yet."""
    req = PreemptionRequest.objects.create(
        beneficiary=beneficiary,
        target_node=node,
        pool=pool,
        status=PreemptionStatus.PENDING,
    )
    flipped = []
    for v in victims:
        PreemptionVictim.objects.create(
            request=req, job=v, gpu_count=victim_gpus[v.id]
        )
        # Conditional RUNNING -> PREEMPTING.
        moved = Job.objects.filter(pk=v.pk, state=JobState.RUNNING).update(
            state=JobState.PREEMPTING
        )
        if moved:
            flipped.append(v.id)
    log_decision(
        DecisionKind.PREEMPT_REQUESTED,
        f"Job #{beneficiary.id} (p={beneficiary.priority}) preempting "
        f"{len(victims)} job(s) on '{node.hostname}'; "
        f"waiting for GPU release before placement",
        pool=pool,
        job=beneficiary,
        node=node,
        preemption=req,
        detail={
            "beneficiary": {
                "id": beneficiary.id,
                "priority": beneficiary.priority,
                "min_gpus": beneficiary.min_gpus,
                "max_gpus": beneficiary.max_gpus,
            },
            "target_node": node.hostname,
            "victims": [
                {"job_id": v.id, "priority": v.priority, "gpu_count": victim_gpus[v.id]}
                for v in victims
            ],
            "rule": "priority>=8 may preempt priority<=3",
        },
    )
    return req


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def tick(clock):
    """Run one scheduling round across every pool. Returns a stats summary."""
    now = clock.now()
    cfg = sched_config()
    timeout = cfg["HEARTBEAT_TIMEOUT_SECONDS"]
    queue_timeout = cfg["QUEUE_TIMEOUT_SECONDS"]
    grace = cfg["PREEMPT_GRACE_SECONDS"]

    stats = {
        "at": now.isoformat(),
        "pools_processed": 0,
        "lost_nodes": 0,
        "timeouts": 0,
        "placements": 0,
        "preemptions_started": 0,
        "preemptions_released": 0,
        "preemption_failures": 0,
        "forced_preemptions": 0,
    }

    pool_ids = list(
        ResourcePool.objects.order_by("id").values_list("id", flat=True)
    )
    for pool_id in pool_ids:
        with transaction.atomic():
            # Pool row lock serializes ALL schedulers for this pool.
            pool = ResourcePool.objects.select_for_update().get(pk=pool_id)
            _phase_node_liveness(pool, now, timeout, stats)
            _phase_queue_timeouts(pool, now, queue_timeout, stats)
            _phase_preemption_followup(pool, now, grace, stats)
            _phase_place(pool, now, timeout, stats)
            stats["pools_processed"] += 1

    return stats
