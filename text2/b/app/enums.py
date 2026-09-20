"""Domain enumerations shared by models, schemas and services."""
from __future__ import annotations

import enum


class PlanVersionStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class TaskStatus(str, enum.Enum):
    """Lifecycle of a generated task.

    PENDING      - generated, no worker assigned yet (or invitation expired)
    INVITED      - an open invitation exists for one worker
    ASSIGNED     - worker accepted; work has not started
    IN_PROGRESS  - start time passed for an accepted assignment
    COMPLETED    - marked done (by test/seed/admin flow)
    CANCELLED    - replaced by a newer plan version or manually cancelled
    """

    PENDING = "pending"
    INVITED = "invited"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# Statuses that count as "not started yet" and may therefore be replaced when
# a care plan is revised. Even an *accepted* task whose start time has not
# arrived is protected, because someone is already committed to it.
REPLACEABLE_TASK_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.INVITED})
ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.INVITED,
        TaskStatus.ASSIGNED,
        TaskStatus.IN_PROGRESS,
    }
)
COMPLETED_TASK_STATUSES = frozenset({TaskStatus.COMPLETED})


class AssignmentStatus(str, enum.Enum):
    """Lifecycle of a single worker invitation/assignment on a task."""

    PENDING = "pending"        # invitation sent, waiting for accept
    ACCEPTED = "accepted"      # worker accepted; holds the slot
    EXPIRED = "expired"        # TTL elapsed without acceptance
    DECLINED = "declined"      # worker explicitly declined
    CANCELLED = "cancelled"    # task cancelled / coordinator reassignment
    SUPERSEDED = "superseded"  # replaced by a later assignment on the task


class EventType(str, enum.Enum):
    PLAN_CREATED = "plan_created"
    PLAN_REVISED = "plan_revised"
    TASK_GENERATED = "task_generated"
    TASK_CANCELLED = "task_cancelled"
    INVITATION_SENT = "invitation_sent"
    INVITATION_ACCEPTED = "invitation_accepted"
    INVITATION_DECLINED = "invitation_declined"
    INVITATION_EXPIRED = "invitation_expired"
    ASSIGNMENT_CANCELLED = "assignment_cancelled"
    MANUAL_ASSIGN = "manual_assign"
    MANUAL_UNASSIGN = "manual_unassign"
    MANUAL_RESCHEDULE = "manual_reschedule"
