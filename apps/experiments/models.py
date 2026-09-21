"""A/B experiments with stable 50/50 assignment.

Assignment model
----------------
Each experiment points at two *immutable* ConfigVersions (variant A and
variant B). Users are bucketed deterministically by

    byte0 = sha256(f"{experiment.salt}:{user_key_hash}")[0]
    variant = "A" if byte0 < 128 else "B"

The salt is unique per experiment, so:

* the same user key is always in the same variant for the experiment's whole
  lifetime (stable assignment);
* reusing a user key across experiments spreads them independently;
* publishing a *new* config version changes nothing — the experiment still
  references the original version objects, historical events keep the version
  they recorded, and assignments are never recomputed ("配置变更不能污染历史
  组别").

One running experiment per placement at a time (enforced in the service under
a row lock).
"""
import hashlib
import uuid

from django.db import models

from apps.catalog.models import ConfigVersion, Placement

VARIANT_A = "A"
VARIANT_B = "B"
VARIANTS = (VARIANT_A, VARIANT_B)


class Experiment(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        STOPPED = "stopped", "Stopped"

    placement = models.ForeignKey(
        Placement, on_delete=models.PROTECT, related_name="experiments"
    )
    name = models.CharField(max_length=120)
    # Independent per experiment; included in the assignment hash.
    salt = models.UUIDField(default=uuid.uuid4, editable=False)
    variant_a_version = models.ForeignKey(
        ConfigVersion, on_delete=models.PROTECT, related_name="experiments_as_a"
    )
    variant_b_version = models.ForeignKey(
        ConfigVersion, on_delete=models.PROTECT, related_name="experiments_as_b"
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RUNNING
    )
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="experiments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    stopped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["placement"],
                condition=models.Q(status="running"),
                name="uniq_running_experiment_per_placement",
            )
        ]

    def bucket(self, user_key_hash: str) -> str:
        digest = hashlib.sha256(
            f"{self.salt}:{user_key_hash.strip().lower()}".encode("utf-8")
        ).digest()
        return VARIANT_A if digest[0] < 128 else VARIANT_B


class ExperimentAssignment(models.Model):
    """Frozen bucket membership. Append-once: the first resolution wins and
    the row is never updated, which is what makes historical cohorts stable.
    """

    experiment = models.ForeignKey(
        Experiment, on_delete=models.PROTECT, related_name="assignments"
    )
    user_key_hash = models.CharField(max_length=64)
    variant = models.CharField(max_length=1, choices=[(v, v) for v in VARIANTS])
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["experiment", "user_key_hash"],
                name="uniq_assignment_per_experiment_user",
            )
        ]
        indexes = [models.Index(fields=["experiment", "variant"])]
