"""FieldSnap 数据模型。

设计要点
========
* 表单模板按 (项目, 编码, 版本号) 版本化；已发布版本行不可变，只能弃用。
* 现场记录只保存"指针"状态；所有实际内容以只追加方式保存在 RecordVersion，
  因此同一 UUID 的多次（冲突）提交都会保留，不会互相覆盖。
* ChangeLog 是增量同步的统一事件源（outbox），使用单调 BIGINT 主键做游标。
* SyncBatch/SyncItem 保留每次推送同步的条目级结果，支持断网重试与对账。
"""
from django.conf import settings
from django.db import models


class Project(models.Model):
    code = models.CharField("项目编码", max_length=64, unique=True)
    name = models.CharField("项目名称", max_length=200)
    is_active = models.BooleanField("启用中", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "项目"
        verbose_name_plural = verbose_name
        ordering = ("code",)

    def __str__(self):
        return f"{self.code} {self.name}"


class Team(models.Model):
    code = models.CharField("班组编码", max_length=64, unique=True)
    name = models.CharField("班组名称", max_length=200)
    projects = models.ManyToManyField(
        Project, related_name="teams", verbose_name="关联项目", blank=True
    )
    supervisors = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="supervised_teams",
        verbose_name="主管",
        blank=True,
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="teams",
        verbose_name="班组成员",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "班组"
        verbose_name_plural = verbose_name
        ordering = ("code",)

    def __str__(self):
        return f"{self.code} {self.name}"


class Profile(models.Model):
    """用户扩展档案：角色 + 工作人员可访问项目的显式分配。"""

    class Role(models.TextChoices):
        WORKER = "worker", "工作人员"
        SUPERVISOR = "supervisor", "主管"
        ADMIN = "admin", "管理员"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    role = models.CharField(
        "角色", max_length=20, choices=Role.choices, default=Role.WORKER
    )
    assigned_projects = models.ManyToManyField(
        Project,
        related_name="assigned_users",
        verbose_name="分配项目",
        blank=True,
    )

    class Meta:
        verbose_name = "用户档案"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.user.username}({self.role})"


def _default_schema():
    return {"fields": [], "version_notes": ""}


class FormTemplate(models.Model):
    """版本化表单模板。

    schema 结构::

        {
          "fields": [
            {"key": "site_name", "label": "工地", "type": "text", "required": true},
            {"key": "temp", "label": "温度", "type": "number",
             "min": -50, "max": 100},
            {"key": "phase", "label": "阶段", "type": "enum",
             "options": ["a", "b", "c"]},
            {"key": "day", "label": "日期", "type": "date"},
            {"key": "reason", "label": "异常原因", "type": "text",
             "required_if": {"field": "phase", "op": "eq", "value": "b"}}
          ]
        }

    published_at 非空即视为已发布；已发布行除 status 外不可修改。
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        DEPRECATED = "deprecated", "已弃用"

    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="templates"
    )
    code = models.CharField("模板编码", max_length=64)
    version = models.PositiveIntegerField("版本号")
    name = models.CharField("模板名称", max_length=200)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    schema = models.JSONField("表单定义", default=_default_schema)
    # 发布时快照的兼容策略：被删除字段加入 legacy_fields，旧设备仍可提交这些字段。
    legacy_fields = models.JSONField("遗留字段白名单", default=list, blank=True)
    version_notes = models.TextField("版本说明", blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_templates",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    deprecated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "表单模板"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["project", "code", "version"],
                name="uniq_template_project_code_version",
            )
        ]
        ordering = ("project_id", "code", "-version")
        indexes = [
            models.Index(fields=["code", "status"]),
        ]

    @property
    def is_published(self):
        return self.status == self.Status.PUBLISHED

    @property
    def field_defs(self):
        return (self.schema or {}).get("fields", [])

    def __str__(self):
        return f"{self.code} v{self.version} [{self.status}]"


class FormRecord(models.Model):
    """现场表单记录（逻辑实体，UUID 由客户端生成）。"""

    class Status(models.TextChoices):
        ACTIVE = "active", "正常"
        IN_CONFLICT = "in_conflict", "冲突待解决"
        RESOLVED = "resolved", "冲突已解决"
        DELETED = "deleted", "已删除"

    uuid = models.UUIDField("记录 UUID", db_index=True)
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="records"
    )
    template = models.ForeignKey(
        FormTemplate, on_delete=models.PROTECT, related_name="records"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    current_version = models.ForeignKey(
        "RecordVersion",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "现场记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["project", "uuid"], name="uniq_record_project_uuid"
            )
        ]
        indexes = [
            models.Index(fields=["project", "status"]),
        ]

    def __str__(self):
        return f"{self.uuid} [{self.status}]"


class RecordVersion(models.Model):
    """记录的一个内容版本（只追加，永不就地修改）。

    同一客户端对同一 UUID 的重复提交：content_hash 相同即幂等重试，
    直接返回该版本的原结果；hash 不同则新增一个候选版本并产生冲突，
    已有内容永远不会被覆盖。
    """

    class Status(models.TextChoices):
        ACCEPTED = "accepted", "已接受（当前）"
        CANDIDATE = "candidate", "冲突候选"
        SUPERSEDED = "superseded", "已被取代"

    record = models.ForeignKey(
        FormRecord, on_delete=models.PROTECT, related_name="versions"
    )
    template = models.ForeignKey(
        FormTemplate, on_delete=models.PROTECT, related_name="record_versions"
    )
    content = models.JSONField("提交内容")
    content_hash = models.CharField("内容哈希", max_length=64, db_index=True)
    client_record_version = models.PositiveIntegerField("客户端记录版本")
    collected_at = models.DateTimeField("采集时间")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="record_versions",
    )
    device_id = models.CharField("设备标识", max_length=128, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACCEPTED
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "记录版本"
        verbose_name_plural = verbose_name
        get_latest_by = "id"
        ordering = ("id",)
        indexes = [
            # 幂等查询：同记录同内容同客户端版本
            models.Index(
                fields=["record", "content_hash", "client_record_version"],
                name="idx_rv_idempotent",
            )
        ]

    def __str__(self):
        return f"{self.record_id}:{self.content_hash[:10]}@{self.client_record_version}"


class Conflict(models.Model):
    """同 UUID 不同内容的并发修改；保留双方版本，由主管显式解决。"""

    class Status(models.TextChoices):
        OPEN = "open", "待解决"
        RESOLVED = "resolved", "已解决"

    record = models.ForeignKey(
        FormRecord, on_delete=models.PROTECT, related_name="conflicts"
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="conflicts"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    versions = models.ManyToManyField(RecordVersion, related_name="conflicts")
    winning_version = models.ForeignKey(
        RecordVersion,
        on_delete=models.PROTECT,
        related_name="won_conflicts",
        null=True,
        blank=True,
    )
    resolution_note = models.TextField("解决依据", blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="resolved_conflicts",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "冲突"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["record"],
                condition=models.Q(status="open"),
                name="uniq_one_open_conflict_per_record",
            )
        ]
        ordering = ("-created_at",)

    def __str__(self):
        return f"Conflict({self.record_id}) {self.status}"


class ChangeLog(models.Model):
    """增量同步事件源。单调自增 BIGINT 主键即稳定游标。"""

    class Kind(models.TextChoices):
        RECORD_UPSERTED = "record_upserted", "记录写入/更新"
        CONFLICT_DETECTED = "conflict_detected", "发现冲突"
        CONFLICT_RESOLVED = "conflict_resolved", "冲突已解决"
        RECORD_DELETED = "record_deleted", "记录删除（墓碑）"

    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="change_logs"
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    record = models.ForeignKey(
        FormRecord, on_delete=models.PROTECT, related_name="change_logs"
    )
    record_version = models.ForeignKey(
        RecordVersion, on_delete=models.PROTECT, null=True, blank=True
    )
    conflict = models.ForeignKey(
        Conflict, on_delete=models.PROTECT, null=True, blank=True
    )
    # 事件发生时渲染好的快照（读取始终按提交当时的模板版本）。
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "变更日志"
        verbose_name_plural = verbose_name
        ordering = ("id",)
        indexes = [
            models.Index(fields=["project", "id"]),
        ]


class SyncBatch(models.Model):
    """一次批量推送同步（客户端可用 client_batch_id 安全重试整批）。"""

    class Status(models.TextChoices):
        COMPLETED = "completed", "已完成"
        PROCESSING = "processing", "处理中（异常中断）"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sync_batches"
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="sync_batches"
    )
    client_batch_id = models.CharField("客户端批次 ID", max_length=128)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.COMPLETED
    )
    item_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "同步批次"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client_batch_id"],
                name="uniq_syncbatch_user_clientbatch",
            )
        ]
        indexes = [models.Index(fields=["-created_at"])]


class SyncItem(models.Model):
    """批次内每个条目的处理结果（条目级结果持久化，断网重试可对账）。"""

    class Status(models.TextChoices):
        CREATED = "created", "新建成功"
        UPDATED = "updated", "接受新版本"
        IDEMPOTENT = "idempotent", "幂等重放（同 UUID 同内容）"
        CONFLICT = "conflict", "内容冲突，已保留双方版本"
        ERROR = "error", "校验/权限错误"

    batch = models.ForeignKey(
        SyncBatch, on_delete=models.CASCADE, related_name="items"
    )
    index = models.PositiveIntegerField("批次内序号")
    uuid = models.UUIDField(null=True, blank=True)
    raw_uuid = models.CharField("原始 UUID 字符串", max_length=64, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices)
    code = models.CharField("机器可读码", max_length=64)
    message = models.TextField(blank=True, default="")
    record_version = models.ForeignKey(
        RecordVersion, on_delete=models.SET_NULL, null=True, blank=True
    )
    conflict = models.ForeignKey(Conflict, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "同步条目结果"
        verbose_name_plural = verbose_name
        ordering = ("batch_id", "index")


class SyncState(models.Model):
    """每个用户+项目的增量拉取游标（服务重启后可继续同步）。"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sync_states"
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="sync_states"
    )
    cursor = models.BigIntegerField("已确认游标", default=0)
    hwm = models.BigIntegerField("本轮快照高水位", null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "同步游标"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["user", "project"], name="uniq_syncstate_user_project"
            )
        ]
