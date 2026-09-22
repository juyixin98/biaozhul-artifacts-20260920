"""A/B (50/50) experiments over two immutable waterfall configurations.

Design guarantees
-----------------
* Each variant is bound to a *published, immutable* ``WaterfallVersion``.
  Bound versions cannot be swapped after the experiment starts -- this is what
  prevents configuration changes from polluting historical groups. Editing an
  experiment's config means publishing fresh versions and creating a new
  experiment.
* Grouping is a stable hash of ``(experiment.public_key, user_key)``; the same
  user maps to the same variant for the experiment's entire life, independently
  of any config version. Assignments are persisted only as an observability
  aid -- assignment is always recomputed deterministically, so a user observed
  before and after a redeploy still lands in the same group.
* Impressions store ``(experiment, variant, version)`` denormalized at ingest
  time, so stats are computed against exactly what the user saw.
"""
import secrets

from django.db import models

from applications.models import Placement
from waterfall.models import WaterfallVersion


def _new_public_key() -> str:
    return "exp_" + secrets.token_hex(8)


class Experiment(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        RUNNING = "running", "Running"
        STOPPED = "stopped", "Stopped"

    placement = models.ForeignKey(
        Placement, on_delete=models.CASCADE, related_name="experiments"
    )
    name = models.CharField(max_length=255)
    public_key = models.SlugField(max_length=40, unique=True, default=_new_public_key)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def developer_id(self):
        return self.placement.app.developer_id

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.public_key} ({self.status})"


class VariantVersion(models.Model):
    """Binds variant 'A' / 'B' to one immutable waterfall version."""

    experiment = models.ForeignKey(
        Experiment, on_delete=models.CASCADE, related_name="variants"
    )
    variant = models.CharField(max_length=1, choices=[("A", "A"), ("B", "B")])
    version = models.ForeignKey(
        WaterfallVersion, on_delete=models.PROTECT,
        related_name="experiment_variants",
        help_text="Must be a published version of the experiment's placement.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["experiment", "variant"], name="uniq_variant_per_experiment"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.experiment_id}:{self.variant}->v{self.version.version_number}"


class Assignment(models.Model):
    """Persisted record of a stable bucket assignment (observability only)."""

    experiment = models.ForeignKey(
        Experiment, on_delete=models.CASCADE, related_name="assignments"
    )
    user_key_hash = models.CharField(max_length=64)
    variant = models.CharField(max_length=1, choices=[("A", "A"), ("B", "B")])
    first_seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["experiment", "user_key_hash"],
                name="uniq_assignment_per_experiment_user",
            ),
        ]
        indexes = [
            models.Index(fields=["experiment", "variant"]),
        ]
