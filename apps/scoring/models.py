"""Scoring runs and per-network scores.

A ScoreRun covers one fixed 30-minute *period* aligned to the UTC clock
(periods start at :00 and :30). Its ``window_start`` is the beginning of the
trailing 7-day event window used for the calculation (period_start - 7 days).

Idempotency
-----------
(period_start) is unique. Re-running the same period returns the existing run
unchanged unless ``force=True``. The score is a *pure function of the event
table and the window*: late-arriving events only change a run if it is
explicitly recomputed (``backfill``), and recomputation fully replaces the
run's NetworkScore rows inside one transaction before the run is marked
complete, so readers never see half-written scores.

Active switch
-------------
``is_active`` marks exactly one run per placement as the currently usable
score set. After a new run finishes, active status is swapped inside the same
transaction ("新版本须完整生成后原子切换").
"""
from django.db import models


class ScoreRunStatus(models.TextChoices):
    RUNNING = "running", "Running"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"


class ScoreRun(models.Model):
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=ScoreRunStatus.choices,
        default=ScoreRunStatus.RUNNING,
    )
    is_active = models.BooleanField(default=False)
    # sha256 over the deterministic ordered score rows; lets us verify that
    # two runs of the same window produced identical output.
    checksum = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["period_start"], name="uniq_scoring_period"
            )
        ]
        ordering = ["-period_start"]


class NetworkScore(models.Model):
    run = models.ForeignKey(
        ScoreRun, on_delete=models.CASCADE, related_name="network_scores"
    )
    placement = models.ForeignKey(
        "catalog.Placement",
        on_delete=models.CASCADE,
        related_name="network_scores",
    )
    network = models.ForeignKey(
        "catalog.AdNetwork", on_delete=models.PROTECT, related_name="network_scores"
    )

    # Raw inputs (window aggregates).
    impressions = models.PositiveIntegerField(default=0)
    fills = models.PositiveIntegerField(default=0)
    failures = models.PositiveIntegerField(default=0)
    revenue = models.DecimalField(max_digits=20, decimal_places=6, default=0)

    # Raw component metrics in [0, 1] (eCPM normalized, see service).
    fill_rate = models.DecimalField(max_digits=8, decimal_places=6, null=True)
    ecpm = models.DecimalField(max_digits=20, decimal_places=6, null=True)
    reliability = models.DecimalField(max_digits=8, decimal_places=6, null=True)

    # Normalized components (NULL => treated as neutral 0.5 in the weighted
    # sum; the raw columns stay NULL so missing samples are distinguishable).
    fill_rate_norm = models.DecimalField(max_digits=8, decimal_places=6, null=True)
    ecpm_norm = models.DecimalField(max_digits=8, decimal_places=6, null=True)
    reliability_norm = models.DecimalField(
        max_digits=8, decimal_places=6, null=True
    )

    score = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    rank = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["run_id", "placement_id", "rank"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "placement", "network"],
                name="uniq_score_run_placement_network",
            )
        ]
        indexes = [
            # Fast lookup of the active score set for a placement.
            models.Index(fields=["placement", "run"], name="ix_score_placement_run"),
        ]
