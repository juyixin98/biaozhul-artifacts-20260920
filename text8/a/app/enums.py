"""流程引擎状态常量。不使用数据库枚举类型，便于迁移。"""


class InstanceStatus:
    RUNNING = "running"
    COMPLETED = "completed"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"

    CLOSED = {COMPLETED, REJECTED, WITHDRAWN}


class TaskStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELED = "canceled"


class VersionStatus:
    DRAFT = "draft"
    PUBLISHED = "published"


class EscalationStatus:
    PENDING = "pending"
    DONE = "done"
    CANCELED = "canceled"


class EventType:
    START = "start"
    TASK_APPROVE = "task_approve"
    TASK_REJECT = "task_reject"
    TASKS_CANCELED = "tasks_canceled"
    NODE_APPROVE = "node_approve"
    BRANCH = "branch"
    ESCALATE = "escalate"
    WITHDRAW = "withdraw"
    REJECT = "reject"
    COMPLETE = "complete"
