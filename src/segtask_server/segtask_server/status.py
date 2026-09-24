"""Goal status constants shared with the SegmentTask action definition.

These integers MUST match the STATUS_* constants declared in
``segtask_msgs/action/SegmentTask.action``. Terminal statuses are stored in
SQLite and never transition again; that is the single source of truth for
who won a cancel/complete race.
"""

PENDING = 0
RUNNING = 1
CANCELING = 2
RECOVERING = 3
SUCCEEDED = 4
CANCELED = 5
ABORTED = 6

TERMINAL_STATUSES = (SUCCEEDED, CANCELED, ABORTED)
NON_TERMINAL_STATUSES = (PENDING, RUNNING, CANCELING, RECOVERING)

STATUS_NAMES = {
    PENDING: 'PENDING',
    RUNNING: 'RUNNING',
    CANCELING: 'CANCELING',
    RECOVERING: 'RECOVERING',
    SUCCEEDED: 'SUCCEEDED',
    CANCELED: 'CANCELED',
    ABORTED: 'ABORTED',
}

# Result codes carried by the action result body.
CODE_SUCCEEDED = 0
CODE_CANCELED = 1
CODE_ABORTED = 2
CODE_REPLAYED = 3   # goal_id was already terminal; stored outcome returned
CODE_FAILED = 4

# Recovery policies / modes.
POLICY_RESUME = 'resume'
POLICY_ABORT = 'abort'
MODE_MANUAL = 'manual'
VALID_POLICIES = (POLICY_RESUME, POLICY_ABORT)
VALID_MODES = (POLICY_RESUME, POLICY_ABORT, MODE_MANUAL)
