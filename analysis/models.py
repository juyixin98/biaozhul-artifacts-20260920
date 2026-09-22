from django.conf import settings
from django.db import models


class Course(models.Model):
    """A course is the isolation boundary: teachers only see their courses."""

    name = models.CharField(max_length=200)
    # The teacher who created the course; also a member of `teachers`.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_courses",
    )
    teachers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="courses",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class SubmissionBatch(models.Model):
    """One multipart upload: up to 100 files for a single course."""

    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="batches")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="batches"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]


class Document(models.Model):
    """
    Canonical text within a course. Deduplication is scoped to
    (course, content_sha256): the *same text uploaded to a different course*
    is a separate row with separate permissions and results.
    """

    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="documents")
    batch = models.ForeignKey(
        SubmissionBatch, on_delete=models.PROTECT, related_name="documents"
    )
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.PositiveIntegerField()
    # SHA-256 over the normalised extracted plain text.
    content_sha256 = models.CharField(max_length=64, db_index=True)
    text = models.TextField()
    char_count = models.PositiveIntegerField(default=0)
    word_count = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="documents"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "content_sha256"],
                name="uniq_document_per_course_content",
            )
        ]
        indexes = [models.Index(fields=["course", "created_at"])]


class UploadAttempt(models.Model):
    """
    One row per file slot in an upload, including duplicates and rejected
    files. Keeps an independent submission record even when the text already
    exists in another course (or twice in the same batch).
    """

    class Status(models.TextChoices):
        CREATED = "created", "Created new document"
        DUPLICATE = "duplicate", "Duplicate of an existing document in this course"
        REJECTED = "rejected", "Rejected before storage"

    batch = models.ForeignKey(
        SubmissionBatch, on_delete=models.CASCADE, related_name="attempts"
    )
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="attempts")
    filename = models.CharField(max_length=255)
    size_bytes = models.PositiveIntegerField(default=0)
    content_sha256 = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices)
    # Set for created/duplicate; None for rejected files (too large, bad type,
    # unreadable archive).
    document = models.ForeignKey(
        Document, on_delete=models.SET_NULL, null=True, blank=True, related_name="attempts"
    )
    detail = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class AnalysisTask(models.Model):
    """Database work-queue item. See analysis.queue for the claim protocol."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for a worker"
        RUNNING = "running", "Claimed by a worker"
        SUCCEEDED = "succeeded", "Result stored"
        FAILED = "failed", "Permanently failed (max attempts reached)"

    class Stage(models.TextChoices):
        QUEUED = "queued", "Queued"
        EXTRACT = "extract", "Text normalisation"
        PARSE = "parse", "Linguistic parsing (spaCy/NLTK)"
        METRICS = "metrics", "Metric computation"
        STYLE = "style", "Style similarity against course samples"
        PERSIST = "persist", "Storing the versioned result"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="tasks")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)

    worker_id = models.CharField(max_length=64, blank=True, default="")
    leased_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Bumped every time the task is (re)claimed. A result write carrying an old
    # generation is refused, so a zombie worker resuming after a lease expiry
    # can never overwrite the newer worker's result.
    generation = models.PositiveIntegerField(default=0)

    error_stage = models.CharField(max_length=20, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status", "lease_expires_at"]),
        ]

    def __str__(self):
        return f"AnalysisTask#{self.pk} doc={self.document_id} {self.status}"


class AnalysisResult(models.Model):
    """
    Immutable, append-only result version for one document. `version` starts
    at 1 and increases on every rerun. The algorithm version and the exact
    input digest are bound into the row, so old numbers stay interpretable.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="results"
    )
    version = models.PositiveIntegerField()
    algorithm_version = models.CharField(max_length=20)
    input_sha256 = models.CharField(max_length=64)
    metrics_json = models.JSONField()
    # Style vector used for similarity; later runs compare against it.
    style_vector = models.JSONField(null=True, blank=True)
    task = models.OneToOneField(
        AnalysisTask, on_delete=models.PROTECT, related_name="result"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version"], name="uniq_result_version"
            ),
        ]
        indexes = [models.Index(fields=["document", "-version"])]
