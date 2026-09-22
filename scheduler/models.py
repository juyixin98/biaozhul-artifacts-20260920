"""
Domain models for the simulated GPU scheduling engine.

Entity overview
---------------
ResourcePool  - quota boundary: max total GPUs and max concurrently running jobs.
Node          - a GPU machine reporting GPU count, per-card memory and heartbeats.
Job           - a queued/running/... training job request (simulated).
Allocation    - the authoritative record that a Job occupies GPUs on a Node.
PreemptionRequest - a high-priority job's attempt to evict low-priority victims.
SchedulingDecision - audit log of every placement / refusal / settlement.

Concurrency correctness lives in ``scheduler.services`` / ``scheduler.engine``:
GPU counters are only mutated while holding ordered row locks, and every job
state transition is a conditional (compare-and-set) UPDATE so that competing
scheduler/workers can never settle the same job twice.
"""
from django.db import models


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class NodeAdminState(models.TextChoices):
    """Operator-controlled state."""

    ONLINE = "ONLINE", "Online (accepts new jobs)"
    DRAINING = "DRAINING", "Draining (no new jobs; existing jobs keep running)"
    OFFLINE = "OFFLINE", "Offline (administratively down)"


# Effective, scheduler-facing node state (computed from admin state + heartbeat).
class NodeState(models.TextChoices):
    AVAILABLE = "AVAILABLE", "Available"
    OCCUPIED = "OCCUPIED", "Occupied"
    DRAINING = "DRAINING", "Draining"
    OFFLINE = "OFFLINE", "Offline"


class JobState(models.TextChoices):
    PENDING = "PENDING", "Queued / waiting to be scheduled"
    RUNNING = "RUNNING", "Running"
    PREEMPTING = "PREEMPTING", "Marked for preemption, expected to release GPUs"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled by user or queue timeout"
    FAILED = "FAILED", "Failed (e.g. its node was lost)"


# Terminal states share one namespace but differ in *why* via ``finish_reason``.
TERMINAL_JOB_STATES = frozenset(
    [JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED]
)
ACTIVE_JOB_STATES = frozenset(
    [JobState.PENDING, JobState.RUNNING, JobState.PREEMPTING]
)


class FinishReason(models.TextChoices):
    COMPLETED = "COMPLETED", "Worker reported completion"
    USER_CANCELLED = "USER_CANCELLED", "Cancelled via API"
    TIMEOUT = "TIMEOUT", "Queued for longer than QUEUE_TIMEOUT_SECONDS"
    PREEMPTED = "PREEMPTED", "Evicted by a higher-priority job"
    NODE_LOST = "NODE_LOST", "Host node went offline unexpectedly"


class PreemptionStatus(models.TextChoices):
    PENDING = "PENDING", "Victims notified, waiting for them to release GPUs"
    RELEASED = "RELEASED", "All victims released; beneficiary may be placed"
    FAILED = "FAILED", "Preemption attempt failed (release timed out / node lost)"


class DecisionKind(models.TextChoices):
    PLACED = "PLACED", "Job placed on a node"
    NO_FIT = "NO_FIT", "Job could not fit any node"
    PREEMPT_REQUESTED = "PREEMPT_REQUESTED", "Preemption initiated"
    PREEMPT_RELEASED = "PREEMPT_RELEASED", "Preempted resources released"
    PREEMPT_FAILED = "PREEMPT_FAILED", "Preemption attempt failed"
    SETTLED = "SETTLED", "A job was settled (complete/cancel/timeout/lost/preempt)"
    NODE_OFFLINE = "NODE_OFFLINE", "A node was marked offline (lost heartbeat/admin)"
    NODE_RECOVERED = "NODE_RECOVERED", "A node's heartbeat recovered"


# ---------------------------------------------------------------------------
# Resource pool & nodes
# ---------------------------------------------------------------------------

class ResourcePool(models.Model):
    name = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=255, blank=True, default="")

    # Quota: total GPUs that may ever be allocated inside the pool, and the
    # maximum number of jobs allowed to run at the same time.
    max_total_gpus = models.PositiveIntegerField()
    max_concurrent_jobs = models.PositiveIntegerField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"Pool({self.name})"


class Node(models.Model):
    pool = models.ForeignKey(
        ResourcePool, on_delete=models.CASCADE, related_name="nodes"
    )
    hostname = models.CharField(max_length=128, unique=True)

    # Hardware report.
    gpu_count = models.PositiveIntegerField()          # physical GPUs on the box
    gpu_memory_mb = models.PositiveIntegerField()      # per-card memory (MiB)

    # Operational state.
    admin_state = models.CharField(
        max_length=16,
        choices=NodeAdminState.choices,
        default=NodeAdminState.ONLINE,
    )
    registered_at = models.DateTimeField(auto_now_add=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    marked_offline_at = models.DateTimeField(null=True, blank=True)

    # Allocation counters (authoritative allocation rows live in Allocation).
    allocated_gpus = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["pool", "admin_state"])]

    def __str__(self):
        return f"Node({self.hostname}, {self.free_gpus}/{self.gpu_count} GPUs free)"

    @property
    def free_gpus(self):
        return self.gpu_count - self.allocated_gpus

    def alive_allocation_total(self):
        """Sum of GPUs over live allocation rows (reconciliation helper)."""
        total = self.allocations.filter(
            job__state__in=[JobState.RUNNING, JobState.PREEMPTING]
        ).aggregate(total=models.Sum("gpu_count"))["total"]
        return total or 0

    def is_heartbeat_stale(self, now, heartbeat_timeout_seconds):
        if not self.last_heartbeat_at:
            return True
        return (now - self.last_heartbeat_at).total_seconds() > heartbeat_timeout_seconds

    def effective_state(self, now, heartbeat_timeout_seconds):
        """
        Resolve the scheduler-facing state.

        - Explicit admin OFFLINE                 -> OFFLINE
        - heartbeat older than the timeout       -> OFFLINE (lost contact)
        - admin DRAINING (but heartbeating)      -> DRAINING (never gets new work)
        - online + heartbeating + GPUs in use    -> OCCUPIED
        - online + heartbeating + free GPUs      -> AVAILABLE
        """
        if self.admin_state == NodeAdminState.OFFLINE:
            return NodeState.OFFLINE
        if self.is_heartbeat_stale(now, heartbeat_timeout_seconds):
            return NodeState.OFFLINE
        if self.admin_state == NodeAdminState.DRAINING:
            return NodeState.DRAINING
        if self.allocated_gpus > 0:
            return NodeState.OCCUPIED
        return NodeState.AVAILABLE

    def accepts_new_jobs(self, now, heartbeat_timeout_seconds):
        """Only a heartbeating, online node receives new allocations."""
        return self.effective_state(now, heartbeat_timeout_seconds) in (
            NodeState.AVAILABLE,
            NodeState.OCCUPIED,
        )


# ---------------------------------------------------------------------------
# Jobs & allocations
# ---------------------------------------------------------------------------

class Job(models.Model):
    pool = models.ForeignKey(
        ResourcePool, on_delete=models.CASCADE, related_name="jobs"
    )
    name = models.CharField(max_length=128)

    # Resource request.
    min_gpus = models.PositiveIntegerField()
    max_gpus = models.PositiveIntegerField()
    gpu_memory_mb = models.PositiveIntegerField()  # required per-card memory
    priority = models.PositiveSmallIntegerField()  # 1..10 (validated at API)

    state = models.CharField(
        max_length=16, choices=JobState.choices, default=JobState.PENDING
    )
    finish_reason = models.CharField(
        max_length=20, choices=FinishReason.choices, blank=True, default=""
    )

    enqueued_at = models.DateTimeField(auto_now_add=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["pool", "state"]),
            models.Index(fields=["state", "priority"]),
        ]

    def __str__(self):
        return f"Job({self.id} {self.name} p={self.priority} {self.state})"


class Allocation(models.Model):
    """Authoritative 'job X occupies n GPUs on node Y' record.

    One row per active placement. The row is deleted atomically when the job
    releases its GPUs, which is the single signal used to free node counters
    and (after restart) to rebuild node counters from surviving rows.
    """

    job = models.OneToOneField(
        Job, on_delete=models.CASCADE, related_name="allocation"
    )
    node = models.ForeignKey(
        Node, on_delete=models.CASCADE, related_name="allocations"
    )
    pool = models.ForeignKey(
        ResourcePool, on_delete=models.CASCADE, related_name="allocations"
    )
    gpu_count = models.PositiveIntegerField()
    placed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


# ---------------------------------------------------------------------------
# Preemption
# ---------------------------------------------------------------------------

class PreemptionRequest(models.Model):
    beneficiary = models.ForeignKey(
        Job, on_delete=models.CASCADE, related_name="preemption_requests"
    )
    target_node = models.ForeignKey(
        Node, on_delete=models.CASCADE, related_name="incoming_preemptions"
    )
    pool = models.ForeignKey(
        ResourcePool, on_delete=models.CASCADE, related_name="preemptions"
    )

    status = models.CharField(
        max_length=12,
        choices=PreemptionStatus.choices,
        default=PreemptionStatus.PENDING,
    )
    fail_reason = models.CharField(max_length=64, blank=True, default="")

    victims = models.ManyToManyField(
        Job, through="PreemptionVictim", related_name="preempted_by_requests"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["pool", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Preemption(#{self.id} job={self.beneficiary_id} {self.status})"


class PreemptionVictim(models.Model):
    request = models.ForeignKey(
        PreemptionRequest, on_delete=models.CASCADE, related_name="victim_rows"
    )
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    # Snapshot of how many GPUs the victim was holding when chosen.
    gpu_count = models.PositiveIntegerField()

    class Meta:
        unique_together = [("request", "job")]


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class SchedulingDecision(models.Model):
    """Immutable audit record: why each scheduling/preemption action happened."""

    kind = models.CharField(max_length=24, choices=DecisionKind.choices)
    pool = models.ForeignKey(
        ResourcePool,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="decisions",
    )
    job = models.ForeignKey(
        Job,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decisions",
    )
    node = models.ForeignKey(
        Node,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decisions",
    )
    preemption = models.ForeignKey(
        PreemptionRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decisions",
    )
    summary = models.CharField(max_length=255)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["kind"]),
        ]

    def __str__(self):
        return f"Decision({self.kind} job={self.job_id} :: {self.summary})"
