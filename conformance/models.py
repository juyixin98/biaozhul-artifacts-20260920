from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models

from .template_def import validate_definition


class Project(models.Model):
    name = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ProjectMembership(models.Model):
    """Analysts may only access projects they hold a membership in."""

    class Role(models.TextChoices):
        ANALYST = "analyst", "Analyst"
        ADMIN = "admin", "Admin"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="memberships"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.ANALYST
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "project"], name="uniq_membership_user_project"
            )
        ]

    def __str__(self):
        return f"{self.user} @ {self.project} ({self.role})"


class ProcessTemplate(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="templates"
    )
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"], name="uniq_template_project_name"
            )
        ]

    def __str__(self):
        return f"{self.project}:{self.name}"


class TemplateVersion(models.Model):
    """One immutable published version of a process template.

    A version starts as a draft; publishing validates the definition and
    freezes it forever. Changes require a new version.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    template = models.ForeignKey(
        ProcessTemplate, on_delete=models.CASCADE, related_name="versions"
    )
    version = models.PositiveIntegerField()
    definition = models.JSONField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["template", "version"], name="uniq_template_version"
            )
        ]
        ordering = ["template", "version"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            old = TemplateVersion.objects.get(pk=self.pk)
            if old.status == self.Status.PUBLISHED:
                if (
                    self.definition != old.definition
                    or self.version != old.version
                    or self.template_id != old.template_id
                ):
                    raise ValidationError(
                        "published template versions are immutable"
                    )
                if self.status != self.Status.PUBLISHED:
                    raise ValidationError(
                        "a published version cannot be unpublished"
                    )
        if self.status == self.Status.PUBLISHED:
            self.definition = validate_definition(self.definition)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.template} v{self.version} ({self.status})"


class Case(models.Model):
    """A process instance, bound to exactly one template version."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="cases"
    )
    case_key = models.CharField(max_length=200)
    template_version = models.ForeignKey(
        TemplateVersion, on_delete=models.PROTECT, related_name="cases"
    )
    needs_analysis = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "case_key"], name="uniq_case_project_key"
            )
        ]

    def __str__(self):
        return f"{self.project}:{self.case_key}"


class ImportBatch(models.Model):
    class Status(models.TextChoices):
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="import_batches"
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    total = models.PositiveIntegerField(default=0)
    inserted = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)


class Event(models.Model):
    """One observed activity occurrence.

    Uniqueness of (project, event_id) and (case, seq) is what makes batch
    import idempotent and lets conflicts be detected deterministically.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="events"
    )
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="events")
    batch = models.ForeignKey(
        ImportBatch, on_delete=models.CASCADE, related_name="events"
    )
    event_id = models.CharField(max_length=200)
    activity = models.CharField(max_length=200)
    occurred_at = models.DateTimeField()
    seq = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "event_id"], name="uniq_event_project_eid"
            ),
            models.UniqueConstraint(fields=["case", "seq"], name="uniq_event_case_seq"),
        ]
        ordering = ["case", "seq"]

    def __str__(self):
        return f"{self.case.case_key}#{self.seq} {self.activity}"


class AnalysisRun(models.Model):
    class Kind(models.TextChoices):
        INCREMENTAL = "incremental", "Incremental"
        FULL = "full", "Full rebuild"

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="analysis_runs"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cases_processed = models.PositiveIntegerField(default=0)


class CaseAnalysis(models.Model):
    """One revision of the replay verdict for a case.

    Every recomputation that changes the outcome appends a new revision and
    flips is_current on the previous one, so old conclusions are preserved
    as a revision history.
    """

    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting for missing events"
        IN_PROGRESS = "in_progress", "In progress, conformant so far"
        CONFORMANT = "conformant", "Conformant"
        NONCONFORMANT = "nonconformant", "Nonconformant"

    case = models.ForeignKey(
        Case, on_delete=models.CASCADE, related_name="analyses"
    )
    run = models.ForeignKey(
        AnalysisRun, on_delete=models.SET_NULL, null=True, related_name="analyses"
    )
    revision = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices)
    missing_seqs = models.JSONField(default=list)
    deviations = models.JSONField(default=list)
    event_count = models.PositiveIntegerField(default=0)
    is_current = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["case", "revision"], name="uniq_analysis_case_revision"
            )
        ]
        ordering = ["case", "revision"]

    def outcome(self):
        return {
            "status": self.status,
            "missing_seqs": self.missing_seqs,
            "deviations": self.deviations,
            "event_count": self.event_count,
        }

    def __str__(self):
        return f"{self.case} r{self.revision}: {self.status}"
