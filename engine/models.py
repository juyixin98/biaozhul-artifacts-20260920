"""数据模型。

关键不变量
----------
* 去重键为 (course, content_hash)：同一份文本在不同课程中的提交与权限完全独立。
* ``AnalysisResult`` 绑定 (submission, input_hash, algorithm_version, version)，
  重跑（输入变化或算法升级）总是产生新版本行，旧版本保留可追溯。
* 队列状态全部落在 ``AnalysisTask`` 上，worker 通过条件 UPDATE（fence token）
  完成/续租，保证多个工作进程不会重复提交结果、过期回收后旧 worker 无法覆盖。
"""

from django.conf import settings
from django.db import models


class Course(models.Model):
    name = models.CharField(max_length=200)
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="courses",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["teacher"])]

    def __str__(self):
        return self.name


class SubmissionBatch(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="batches")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="batches"
    )
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["course", "-created_at"])]


class Submission(models.Model):
    """一次文档提交。原始字节入库，worker 无状态，可横向扩展。"""

    class SourceFormat(models.TextChoices):
        TXT = "txt", "TXT"
        DOCX = "docx", "DOCX"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="submissions")
    batch = models.ForeignKey(
        SubmissionBatch, on_delete=models.CASCADE, related_name="submissions", null=True
    )
    student_label = models.CharField(max_length=200, blank=True, default="")
    filename = models.CharField(max_length=255)
    source_format = models.CharField(max_length=8, choices=SourceFormat.choices)
    raw_content = models.BinaryField()
    # 内容摘要：规范化（解码+统一换行+strip）后文本的 SHA-256 hex
    content_hash = models.CharField(max_length=64, db_index=True)
    char_count = models.PositiveIntegerField(default=0)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["course", "content_hash"],
                name="uniq_content_hash_per_course",
            )
        ]
        indexes = [models.Index(fields=["course", "-created_at"])]


class AnalysisTask(models.Model):
    """数据库队列任务。

    状态机::

        PENDING ──claim──▶ RUNNING ──finish──▶ SUCCEEDED
                            │  └──finish(失败)──▶ FAILED
                            └──租约过期被回收──▶ PENDING（attempts 累加）
        SUCCEEDED/FAILED ──requeue（重跑）──▶ PENDING（新任务行）
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "待领取"
        RUNNING = "RUNNING", "运行中"
        SUCCEEDED = "SUCCEEDED", "成功"
        FAILED = "FAILED", "失败"

    class Stage(models.TextChoices):
        QUEUED = "QUEUED", "排队"
        EXTRACT = "EXTRACT", "文本提取"
        NLP = "NLP", "NLP 标注"
        METRICS = "METRICS", "指标计算"
        STYLE = "STYLE", "风格相似度"
        PERSIST = "PERSIST", "结果落库"

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="tasks")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    progress = models.PositiveSmallIntegerField(default=0)
    stage = models.CharField(max_length=12, choices=Stage.choices, default=Stage.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=settings.TASK_MAX_ATTEMPTS)
    not_before = models.DateTimeField(default=None, null=True, blank=True, db_index=True)

    worker_id = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    # fence token：每次领取/回收 +1；finish 时必须匹配，旧 worker 的提交被拒绝。
    run_token = models.PositiveIntegerField(default=0)

    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    error_detail = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "not_before"]),
            models.Index(fields=["lease_expires_at"]),
        ]


class AnalysisResult(models.Model):
    """版本化分析结果。永不就地改写，重跑只追加新版本。"""

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="results")
    task = models.OneToOneField(AnalysisTask, on_delete=models.PROTECT, related_name="result")
    version = models.PositiveIntegerField()
    input_hash = models.CharField(max_length=64)
    algorithm_version = models.CharField(max_length=40)
    metrics = models.JSONField()
    methodology = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "version"], name="uniq_result_version_per_submission"
            ),
        ]
        indexes = [models.Index(fields=["submission", "-version"])]


class StyleSample(models.Model):
    """教师上传的已知风格样本（作为相似度参照），按课程隔离。"""

    class Label(models.TextChoices):
        STUDENT = "student", "学生样本"
        REFERENCE = "reference", "参考范文"
        KNOWN_AI = "known_ai", "已知 AI 文本样本"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="style_samples")
    label = models.CharField(max_length=16, choices=Label.choices, default=Label.STUDENT)
    name = models.CharField(max_length=200)
    content_hash = models.CharField(max_length=64, db_index=True)
    text = models.TextField()
    # local-style-v1 特征向量（以 dict 存储，避免引入 numpy 依赖）
    feature_version = models.CharField(max_length=40, blank=True, default="")
    feature_vector = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["course", "content_hash", "label"],
                name="uniq_sample_course_hash_label",
            )
        ]
