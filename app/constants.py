"""Shared enumerations and constant values."""

# Node types
NODE_START = "start"
NODE_APPROVAL = "approval"
NODE_CONDITION = "condition"
NODE_END = "end"
NODE_TYPES = {NODE_START, NODE_APPROVAL, NODE_CONDITION, NODE_END}

# Approval strategies
SIGN_ALL = "all"   # 全签：所有审批人通过才推进
SIGN_ANY = "any"   # 任签：任一人通过即推进
SIGN_STRATEGIES = {SIGN_ALL, SIGN_ANY}

# Task statuses
TASK_PENDING = "pending"
TASK_APPROVED = "approved"
TASK_REJECTED = "rejected"
TASK_WITHDRAWN = "withdrawn"
TASK_CLOSED = "closed"  # 任签场景中被他人通过/拒绝后关闭的待办

# Instance statuses
INSTANCE_RUNNING = "running"
INSTANCE_APPROVED = "approved"
INSTANCE_REJECTED = "rejected"
INSTANCE_WITHDRAWN = "withdrawn"
INSTANCE_TERMINAL = {INSTANCE_APPROVED, INSTANCE_REJECTED, INSTANCE_WITHDRAWN}

# Audit event types
AUDIT_TEMPLATE_PUBLISHED = "template_published"
AUDIT_TEMPLATE_ROLLED_BACK = "template_rolled_back"
AUDIT_STARTED = "started"
AUDIT_APPROVED = "approved"
AUDIT_REJECTED = "rejected"
AUDIT_WITHDRAWN = "withdrawn"
AUDIT_TASK_CLOSED = "task_closed"
AUDIT_ESCALATED = "escalated"
AUDIT_ENTER_NODE = "enter_node"

# Decision actions on pending tasks
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTIONS = {ACTION_APPROVE, ACTION_REJECT}
