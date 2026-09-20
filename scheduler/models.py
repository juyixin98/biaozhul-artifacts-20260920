"""
调度引擎数据模型。

只模拟 GPU 资源（按“卡数”记账，不建模单卡实体）与作业状态机，
不涉及真实训练或推荐系统。

关键不变量：
- 一张 Allocation 表示某作业在某节点上占用的 gpu_count 张卡；
- 节点/资源池的“已用卡数”始终由 Allocation 动态聚合，不冗余存储，
  因此服务重启后直接从数据库即可恢复分配关系，不会出现计数漂移；
- 所有状态迁移与占用变更都在带行锁/咨询锁的事务中完成。
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# 资源池
# ---------------------------------------------------------------------------
class ResourcePool(models.Model):
    name = models.CharField("资源池名称", max_length=64, unique=True)
    # 池内所有在线可用节点的 GPU 总和不得超过该配额（0 表示不限制）。
    max_gpus = models.PositiveIntegerField("GPU 总配额", default=0)
    # 池内允许同时处于 ALLOCATED/RUNNING 的作业上限（0 表示不限制）。
    max_running_jobs = models.PositiveIntegerField("最大并发作业数", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "资源池"
        verbose_name_plural = verbose_name
        db_table = "scheduler_resource_pool"

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return self.name


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------
class Node(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available", "可用"
        OCCUPIED = "occupied", "占用"
        DRAINING = "draining", "排空"
        OFFLINE = "offline", "离线"

    # 允许参与调度（可被放入新作业）的状态。
    SCHEDULABLE_STATUSES = (Status.AVAILABLE, Status.OCCUPIED)

    pool = models.ForeignKey(
        ResourcePool, on_delete=models.PROTECT, related_name="nodes"
    )
    name = models.CharField("节点名称", max_length=128, unique=True)
    gpu_count = models.PositiveIntegerField("GPU 数量", default=0)
    gpu_memory_mb = models.PositiveIntegerField("单卡显存 MB", default=0)

    status = models.CharField(
        "节点状态", max_length=16, choices=Status.choices, default=Status.AVAILABLE
    )
    last_heartbeat_at = models.DateTimeField("最近心跳", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "节点"
        verbose_name_plural = verbose_name
        db_table = "scheduler_node"
        indexes = [
            models.Index(fields=["pool", "status"]),
            models.Index(fields=["last_heartbeat_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}[{self.status}]"

    @property
    def is_schedulable(self) -> bool:
        return self.status in self.SCHEDULABLE_STATUSES


# ---------------------------------------------------------------------------
# 作业
# ---------------------------------------------------------------------------
class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        ALLOCATED = "allocated", "已分配待启动"
        RUNNING = "running", "运行中"
        PREEMPTED = "preempted", "被抢占"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"
        TIMED_OUT = "timed_out", "超时取消"

    # 持有 GPU 分配的活动状态。
    ACTIVE_STATUSES = (Status.ALLOCATED, Status.RUNNING)
    # 终态：不可再迁移。
    TERMINAL_STATUSES = (
        Status.PREEMPTED,
        Status.SUCCEEDED,
        Status.FAILED,
        Status.CANCELLED,
        Status.TIMED_OUT,
    )

    pool = models.ForeignKey(
        ResourcePool, on_delete=models.PROTECT, related_name="jobs"
    )
    name = models.CharField("作业名称", max_length=128)

    min_gpus = models.PositiveIntegerField("最少 GPU 数")
    max_gpus = models.PositiveIntegerField("最多 GPU 数")
    gpu_memory_mb = models.PositiveIntegerField("要求单卡显存 MB", default=0)
    # 1—10，数字越大优先级越高。
    priority = models.PositiveSmallIntegerField("优先级", default=5)

    status = models.CharField(
        "作业状态", max_length=16, choices=Status.choices, default=Status.QUEUED
    )

    queued_at = models.DateTimeField("入队时间", default=timezone.now)
    allocated_at = models.DateTimeField("分配时间", null=True, blank=True)
    started_at = models.DateTimeField("开始运行时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)
    # 运行超时（秒）；NULL 表示使用引擎默认值。
    run_timeout_seconds = models.PositiveIntegerField(null=True, blank=True)

    # 抢占/取消/超时等结算原因，便于审计。
    status_reason = models.CharField(
        "状态原因", max_length=255, blank=True, default=""
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "作业"
        verbose_name_plural = verbose_name
        db_table = "scheduler_job"
        indexes = [
            # 调度扫描的主索引：同池排队作业，按优先级/入队时间排序。
            models.Index(fields=["pool", "status", "-priority", "queued_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["queued_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}[{self.status}]"

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# 分配关系（资源占用明细，重启恢复的事实来源）
# ---------------------------------------------------------------------------
class Allocation(models.Model):
    job = models.ForeignKey(
        Job, on_delete=models.PROTECT, related_name="allocations"
    )
    node = models.ForeignKey(
        Node, on_delete=models.PROTECT, related_name="allocations"
    )
    gpu_count = models.PositiveIntegerField("占用 GPU 数")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "分配关系"
        verbose_name_plural = verbose_name
        db_table = "scheduler_allocation"
        indexes = [
            models.Index(fields=["node"]),
            models.Index(fields=["job"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "node"], name="uq_allocation_job_node"
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"job={self.job_id} node={self.node_id} gpus={self.gpu_count}"


# ---------------------------------------------------------------------------
# 调度决策审计（保留每次调度的依据与装箱结果）
# ---------------------------------------------------------------------------
class SchedulingDecision(models.Model):
    class Kind(models.TextChoices):
        SCHEDULE = "schedule", "调度分配"
        SKIP = "skip", "跳过（资源不足/碎片）"
        PREEMPT_ATTEMPT = "preempt_attempt", "尝试抢占"
        PREEMPT_RESULT = "preempt_result", "抢占结果"
        QUEUE_TIMEOUT = "queue_timeout", "排队超时取消"
        RUN_TIMEOUT = "run_timeout", "运行超时取消"
        SETTLE = "settle", "作业结算"
        NODE_OFFLINE = "node_offline", "节点失联"
        NODE_RECOVER = "node_recover", "节点恢复"
        DRAIN = "drain", "节点排空"

    pool = models.ForeignKey(
        ResourcePool, on_delete=models.CASCADE, related_name="decisions", null=True
    )
    job = models.ForeignKey(
        Job, on_delete=models.SET_NULL, related_name="decisions", null=True
    )
    node = models.ForeignKey(
        Node, on_delete=models.SET_NULL, related_name="decisions", null=True
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # 结构化依据：候选节点、空闲容量、装箱计划、抢占受害者等。
    rationale = models.JSONField(default=dict)
    success = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "调度决策"
        verbose_name_plural = verbose_name
        db_table = "scheduler_decision"
        indexes = [
            models.Index(fields=["pool", "kind"]),
            models.Index(fields=["job", "kind"]),
        ]


# ---------------------------------------------------------------------------
# 抢占记录（每次抢占一条，成功/失败及原因都保留）
# ---------------------------------------------------------------------------
class PreemptionRecord(models.Model):
    preemptor = models.ForeignKey(
        Job, on_delete=models.PROTECT, related_name="preemptions_as_preemptor"
    )
    victim = models.ForeignKey(
        Job, on_delete=models.PROTECT, related_name="preemptions_as_victim"
    )
    pool = models.ForeignKey(ResourcePool, on_delete=models.CASCADE)
    success = models.BooleanField(default=False)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "抢占记录"
        verbose_name_plural = verbose_name
        db_table = "scheduler_preemption"
        indexes = [models.Index(fields=["pool", "success"])]
