"""枚举常量。全部使用字符串字面量，避免数据库 enum 迁移问题。"""


class NodeType:
    START = "start"
    APPROVAL = "approval"
    CONDITION = "condition"
    END = "end"


class ApprovalMode:
    ALL = "all"  # 全签（会签）
    ANY = "any"  # 任签（或签）


class TimeoutAction:
    ESCALATE = "escalate"
    AUTO_APPROVE = "auto_approve"
    AUTO_REJECT = "auto_reject"


class TemplateStatus:
    DRAFT = "draft"
    PUBLISHED = "published"


class InstanceStatus:
    RUNNING = "running"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


TERMINAL_STATUSES = {InstanceStatus.APPROVED, InstanceStatus.REJECTED, InstanceStatus.WITHDRAWN}


class TaskStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CLOSED = "closed"  # 任签被他人抢先通过后关闭
    WITHDRAWN = "withdrawn"  # 流程撤回时关闭
    TIMEOUT_CLOSED = "timeout_closed"  # 超时升级后旧待办关闭


class EventType:
    START = "start"
    ENTER_APPROVAL = "enter_approval"
    APPROVE = "approve"
    REJECT = "reject"
    AUTO_APPROVE = "auto_approve"
    AUTO_REJECT = "auto_reject"
    CLOSE_TASKS = "close_tasks"
    ESCALATE = "escalate"
    CONDITION_MATCH = "condition_match"
    CONDITION_DEFAULT = "condition_default"
    COMPLETE = "complete"
    WITHDRAW = "withdraw"


class AuditActorType:
    USER = "user"
    SYSTEM = "system"
    SUBMITTER = "submitter"
