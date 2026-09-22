"""Shared models: configuration change audit trail."""
from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only record of every configuration-mutating action.

    Nothing in the application ever updates or deletes these rows; the queryset
    is filtered in the service layer. Enforcement is additionally backed by the
    service interface (there is intentionally no update/delete API).
    """

    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        PUBLISH = "publish", "Publish"
        DELETE = "delete", "Delete"
        EXPERIMENT_START = "experiment_start", "Experiment start"
        EXPERIMENT_STOP = "experiment_stop", "Experiment stop"

    developer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
        help_text="Who performed the action (null if the account was removed).",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    resource_type = models.CharField(
        max_length=64, help_text="e.g. 'app', 'placement', 'waterfall_version'."
    )
    resource_id = models.CharField(max_length=64)
    # Human readable target, handy when the referenced row is later deleted.
    resource_repr = models.CharField(max_length=255, blank=True, default="")
    diff = models.JSONField(
        default=dict,
        blank=True,
        help_text="Snapshot of changed fields / full payload for creates.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["resource_type", "resource_id"]),
            models.Index(fields=["developer", "-created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.resource_type}#{self.resource_id}"
