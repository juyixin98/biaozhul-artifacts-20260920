"""Scoring runs and per-placement/network score items.

A :class:`ScoreRun` is one 30-minute tick. Its window is the half-open
interval ``[window_start, window_end)`` where ``window_end == slot_start`` and
``window_start = slot_start - 7 days`` (see ``scoring.slots``).

Atomicity
---------
Items are built completely, then the run is flipped to ``finalized`` in the
same transaction. Readers only ever see a finalized run, so a tick is either
wholly available with the previous tick still current, or the new tick is
fully visible -- there is no mixed state.

Idempotency
-----------
There is at most one run per ``slot_start`` (unique constraint). Re-invoking
the scheduler for the same slot returns the stored run byte-for-byte
(identical numbers, ranks, order) instead of recomputing. Late events that
arrive after finalization are picked up by later rolling 7-day windows; they
never mutate a finalized tick.
"""
from django.db import models

from applications.models import AdNetwork, Placement
from common.utils import ZERO, money_field


class ScoreRun(models.Model):
    slot_start = models.DateTimeField(unique=True)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    finalized = models.BooleanField(default=False)
    # Deterministic hash of the item set; identical input -> identical hash.
    items_hash = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    finalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-slot_start"]

    def __str__(self) -> str:  # pragma: no cover
        state = "final" if self.finalized else "pending"
        return f"ScoreRun({self.slot_start:%Y-%m-%d %H:%M} {state})"


class ScoreItem(models.Model):
    run = models.ForeignKey(ScoreRun, on_delete=models.CASCADE, related_name="items")
    placement = models.ForeignKey(
        Placement, on_delete=models.CASCADE, related_name="score_items"
    )
    network = models.ForeignKey(
        AdNetwork, on_delete=models.PROTECT, related_name="score_items"
    )

    # Raw sample counters for the window.
    fills = models.PositiveIntegerField(default=0)
    failures = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    revenue = money_field(default=ZERO)
    opportunities = models.PositiveIntegerField(default=0)

    # Raw metrics (fixed-point fractions, 9 dp; eCPM is USD money, 6 dp).
    fill_rate = models.DecimalField(max_digits=12, decimal_places=9, default=ZERO)
    ecpm = money_field(default=ZERO)
    reliability = models.DecimalField(max_digits=12, decimal_places=9, default=ZERO)

    # Min-max normalized within the placement's candidate set.
    norm_fill_rate = models.DecimalField(max_digits=12, decimal_places=9, default=ZERO)
    norm_ecpm = models.DecimalField(max_digits=12, decimal_places=9, default=ZERO)
    norm_reliability = models.DecimalField(max_digits=12, decimal_places=9, default=ZERO)

    # Weighted total on a 0..100 scale and the rank within the placement
    # (1 = best; ties broken deterministically -- see scoring.services).
    total_score = models.DecimalField(max_digits=12, decimal_places=6, default=ZERO)
    rank = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["run_id", "placement_id", "rank", "network_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "placement", "network"],
                name="uniq_scoreitem_per_run_placement_network",
            ),
        ]
        indexes = [
            models.Index(fields=["placement", "-run", "rank"]),
        ]
