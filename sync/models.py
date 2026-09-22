"""Sync-domain persistence models.

Records are append-only at the *revision* level: a device never overwrites
another device's values. Concurrent edits become sibling revisions flagged as
a conflict until a supervisor merges them. Every revision also appends to
``RecordChange``, the stable change stream consumed by incremental pulls.
"""
import uuid

from django.conf import settings
from django.db import models


class RecordStatus(models.TextChoices):
    OK = "ok", "Current"
    CONFLICT = "conflict", "Needs supervisor resolution"
    DELETED = "deleted", "Deleted"


class RevisionKind(models.TextChoices):
    CREATED = "created", "First accepted revision"
    UPDATED = "updated", "Fast-forward update"
    DIVERGENT = "divergent", "Concurrent edit (same base version, other content)"
    STALE = "stale", "Older version than the current revision"
    RESOLVED = "resolved", "Supervisor resolution"


class ChangeType(models.TextChoices):
    CREATED = "created", "Record created"
    UPDATED = "updated", "Record updated"
    CONFLICT = "conflict", "Conflicting revision stored"
    RESOLVED = "resolved", "Conflict resolved"
    DELETED = "deleted", "Tombstone"


def new_record_uuid():
    return uuid.uuid4()


class FormRecord(models.Model):
    record_uuid = models.UUIDField(unique=True, default=new_record_uuid, db_index=True)
    project = models.ForeignKey(
        "forms.Project", on_delete=models.PROTECT, related_name="records"
    )
    crew = models.ForeignKey(
        "forms.Crew", on_delete=models.PROTECT, related_name="records"
    )
    template = models.ForeignKey(
        "forms.FormTemplate", on_delete=models.PROTECT, related_name="records"
    )
    # Pinned version of the newest accepted content; historical revisions keep
    # their own template_version FK below.
    current_version = models.ForeignKey(
        "forms.FormTemplateVersion",
        on_delete=models.PROTECT,
        related_name="+",
    )
    current_revision = models.ForeignKey(
        "RecordRevision",
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    status = models.CharField(
        max_length=16, choices=RecordStatus.choices, default=RecordStatus.OK
    )
    # The accepted revision the conflicting sibling is measured against.
    conflict_of = models.ForeignKey(
        "RecordRevision",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conflicting_revisions",
    )
    deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_records",
    )

    class Meta:
        indexes = (
            models.Index(fields=("project", "status")),
            models.Index(fields=("crew", "status")),
        )


class RecordRevision(models.Model):
    record = models.ForeignKey(
        FormRecord, on_delete=models.PROTECT, related_name="revisions"
    )
    seq = models.PositiveIntegerField()
    # Record version the device had when it edited (0 on creation). Used to
    # tell fast-forwards (base == head) from divergences (base < head).
    base_version = models.PositiveIntegerField(default=0)
    template_version = models.ForeignKey(
        "forms.FormTemplateVersion", on_delete=models.PROTECT, related_name="revisions"
    )
    data = models.JSONField()
    content_hash = models.CharField(max_length=64)
    collected_at = models.DateTimeField()
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="submitted_revisions",
    )
    kind = models.CharField(max_length=16, choices=RevisionKind.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    # Resolution metadata (only populated on kind=resolved).
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_revisions",
    )
    resolution_note = models.TextField(blank=True)

    class Meta:
        unique_together = (
            ("record", "seq"),
            # Idempotency hinge: identical retries find the existing revision.
            ("record", "content_hash"),
        )
        ordering = ("record", "seq")


class SyncBatch(models.Model):
    """One client sync request; replays are served from stored results."""

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sync_batches"
    )
    client_batch_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    entry_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("submitted_by", "client_batch_id")


class SyncResultEntry(models.Model):
    """Per-entry result, retained so retries/replays return the original answer."""

    class EntryStatus(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        CONFLICT = "conflict", "Conflict"
        REJECTED = "rejected", "Validation rejected"
        ERROR = "error", "Error"

    batch = models.ForeignKey(
        SyncBatch, on_delete=models.CASCADE, related_name="results"
    )
    client_uuid = models.UUIDField()
    status = models.CharField(max_length=16, choices=EntryStatus.choices)
    # Original HTTP-style code carried inside the batch payload (201/200/409/422).
    status_code = models.PositiveIntegerField()
    record_version = models.PositiveIntegerField(null=True, blank=True)
    revision_seq = models.PositiveIntegerField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    errors = models.JSONField(null=True, blank=True)
    conflict_with_seq = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # No unique on (batch, client_uuid): a client may legitimately repeat
        # a UUID inside one batch (immediate retry); both outcome rows are
        # retained in submission order.
        indexes = (models.Index(fields=("batch", "client_uuid")),)


class RecordChange(models.Model):
    """Append-only change stream backing incremental pulls.

    ``id`` is a monotonically increasing BIGINT; a cursor of ``id`` plus
    ``(id >= cursor)`` filtering guarantees no entry is skipped or duplicated
    even when new writes land while a client is paging.
    """

    id = models.BigAutoField(primary_key=True)
    record = models.ForeignKey(
        FormRecord, on_delete=models.PROTECT, related_name="changes"
    )
    revision = models.ForeignKey(
        RecordRevision, on_delete=models.PROTECT, null=True, related_name="changes"
    )
    change_type = models.CharField(max_length=16, choices=ChangeType.choices)
    # Denormalized for permission filtering without a join.
    project = models.ForeignKey(
        "forms.Project", on_delete=models.PROTECT, related_name="changes"
    )
    crew = models.ForeignKey("forms.Crew", on_delete=models.PROTECT, related_name="changes")
    template = models.ForeignKey(
        "forms.FormTemplate", on_delete=models.PROTECT, related_name="changes"
    )
    # Snapshot of the record's status at change time (lets clients render
    # tombstones and conflicts directly from the stream).
    record_status = models.CharField(max_length=16, choices=RecordStatus.choices)
    deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = (
            models.Index(fields=("project", "id")),
            models.Index(fields=("crew", "id")),
            models.Index(fields=("record", "id")),
        )
        ordering = ("id",)
