"""Raw ad lifecycle events. Local only — nothing is ever sent to ad networks.

Idempotency
-----------
``event_id`` is globally unique (client-generated UUID). Re-sending the same
event is a no-op *only* if the payload matches; a mismatched replay is a
conflict (409) so bugs/mix-ups are detectable instead of silently dropped.

Late & out-of-order events are first-class: events up to EVENT_MAX_AGE_DAYS
old are accepted and scoring windows are re-derivable from ``event_time``.
"""
from django.db import models

from apps.catalog.models import AdNetwork, App, Placement


class EventType(models.TextChoices):
    # Waterfall requested the placement.
    IMPRESSION = "impression", "Impression request"
    # A network returned a fill.
    FILL = "fill", "Fill"
    # Revenue reported for a filled impression (fixed-point micro-units).
    REVENUE = "revenue", "Revenue"
    # A network failed to fill / errored.
    FAILURE = "failure", "Failure"


class AdEvent(models.Model):
    event_id = models.CharField(max_length=64, unique=True)
    app = models.ForeignKey(App, on_delete=models.PROTECT, related_name="events")
    placement = models.ForeignKey(
        Placement, on_delete=models.PROTECT, related_name="events"
    )
    network = models.ForeignKey(
        AdNetwork, on_delete=models.PROTECT, related_name="events"
    )
    event_type = models.CharField(max_length=16, choices=EventType.choices)
    event_time = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)

    # Revenue events only.
    revenue = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True
    )
    # Failure events only.
    error_code = models.CharField(max_length=64, blank=True, default="")

    # Attribution captured at ingestion: the immutable config version that was
    # live when the SDK fired, plus optional experiment assignment. These are
    # frozen so later config changes cannot alter historical attribution.
    config_version = models.ForeignKey(
        "catalog.ConfigVersion",
        on_delete=models.PROTECT,
        related_name="events",
        null=True,
        blank=True,
    )
    experiment = models.ForeignKey(
        "experiments.Experiment",
        on_delete=models.PROTECT,
        related_name="events",
        null=True,
        blank=True,
    )
    experiment_variant = models.CharField(max_length=8, blank=True, default="")
    # Stable user key hash as supplied by the SDK.
    user_key_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["event_time"]),
            models.Index(fields=["app", "event_time"]),
            models.Index(fields=["placement", "network", "event_time"]),
            models.Index(fields=["experiment", "experiment_variant"]),
        ]
        ordering = ["event_time"]

    def __str__(self):
        return f"{self.event_type}:{self.event_id}"
