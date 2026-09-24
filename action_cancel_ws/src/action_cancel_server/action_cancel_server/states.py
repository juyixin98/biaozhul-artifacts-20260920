"""Canonical goal states and policy constants.

The numeric values must stay in sync with the constants declared in
``action_cancel_interfaces/action/SegmentedTask.action`` (result section),
``srv/GetGoal.srv`` and ``srv/ListGoals.srv``.
"""

from __future__ import annotations

PENDING = 0
RUNNING = 1
RECOVERABLE = 2
SUCCEEDED = 3
CANCELED = 4
ABORTED = 5
REJECTED = 6

STATE_NAMES = {
    PENDING: "PENDING",
    RUNNING: "RUNNING",
    RECOVERABLE: "RECOVERABLE",
    SUCCEEDED: "SUCCEEDED",
    CANCELED: "CANCELED",
    ABORTED: "ABORTED",
    REJECTED: "REJECTED",
}

TERMINAL = frozenset({SUCCEEDED, CANCELED, ABORTED, REJECTED})
ACTIVE = frozenset({PENDING, RUNNING})  # accept execution on these
# States in which a cancel request is durably admitted.
CANCEL_ADMITTED = frozenset({PENDING, RUNNING, RECOVERABLE})

POLICY_RESUME = 0
POLICY_ABORT = 1

# Machine-readable error codes surfaced in results and history records.
ERR_EMPTY_ID = "EMPTY_GOAL_ID"
ERR_EMPTY_INPUTS = "EMPTY_SEGMENT_INPUTS"
ERR_BAD_ITERATIONS = "BAD_PBKDF2_ITERATIONS"
ERR_BAD_POLICY = "BAD_RECOVERY_POLICY"
ERR_DUP_FINISHED = "DUPLICATE_FINISHED"
ERR_DUP_ACTIVE = "DUPLICATE_ACTIVE"
ERR_DUP_RECOVERABLE = "DUPLICATE_RECOVERABLE"
ERR_CHANGED_PARAMS = "DUPLICATE_ID_CHANGED_PARAMETERS"
ERR_CANCEL_NOT_PENDING = "GOAL_NOT_PENDING_OR_RUNNING"
ERR_GOAL_NOT_FOUND = "GOAL_NOT_FOUND"
ERR_INTEGRITY = "RECOVERY_INTEGRITY_ERROR"
ERR_ABORT_POLICY = "ABORTED_BY_RECOVERY_POLICY"
ERR_CANCEL_DURING_EXECUTION = "CANCELED_DURING_EXECUTION"
ERR_VALIDATION = "VALIDATION_ERROR"


def name(state: int) -> str:
    return STATE_NAMES.get(int(state), f"UNKNOWN({state})")
