"""Raw ad lifecycle events reported by the SDK.

Event types form a local-only lifecycle (the backend never calls ad networks):

* ``impression`` -- an ad was shown;
* ``fill``       -- a network returned a fill;
* ``revenue``    -- a monetized impression carrying a fixed-point amount;
* ``failure``    -- a request failed (``failure_reason`` explains why).

Events may arrive out of order and late; ingestion never rejects an event for
its ``occurred_at`` being old or for a missing predecessor. Idempotency is the
pair (app, event_id).
"""
from django.db import models

from applications.models import AdNetwork, App, Placement
from common.utils import money_field


class EventType(models.TextChoices):
    IMPRESSION = "impression", "Impression"
    FILL = "fill", "Fill"
    REVENUE = "revenue", "Revenue"
    FAILURE = "failure", "Failure"


class FailureReason(models.TextChoices):
    NO_FILL = "no_fill", "No fill"
    TIMEOUT = "timeout", "Timeout"
    ERROR = "error", "Error"
    OTHER = "other", "Other"


# Failure reasons that count as reliability problems. ``no_fill`` is a normal
# commercial outcome and lowers fill rate, not reliability.
ERROR_REASONS = frozenset({FailureReason.TIMEOUT, FailureReason.ERROR})


class Event(models.Model):
    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="events")
    # Client-supplied id; unique within an app -> idempotent ingestion.
    event_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=16, choices=EventType.choices)

    placement = models.ForeignKey(
        Placement, on_delete=models.PROTECT, related_name="events"
    )
    network = models.ForeignKey(
        AdNetwork, on_delete=models.PROTECT, null=True, blank=True,
        related_name="events",
    )
    # The immutable config version that was active when the impression happened.
    # PROTECT keeps historical versions (and the attribution they provide) alive.
    version = models.ForeignKey(
        "waterfall.WaterfallVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
    )

    # Experiment attribution (frozen at impression time).
    experiment = models.ForeignKey(
        "experiments.Experiment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
    )
    variant = models.CharField(max_length=1, null=True, blank=True, choices=[("A", "A"), ("B", "B")])

    amount = money_field(null=True, blank=True, help_text="Revenue amount (USD, 6-dp).")
    failure_reason = models.CharField(
        max_length=16, choices=FailureReason.choices, null=True, blank=True
    )

    user_key_hash = models.CharField(
        max_length=64, null=True, blank=True,
        help_text="SHA-256 of the SDK user key; raw keys are never stored.",
    )

    occurred_at = models.DateTimeField(
        help_text="Client timestamp of when the event happened."
    )
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["app", "event_id"], name="uniq_event_id_per_app"
            ),
        ]
        indexes = [
            models.Index(fields=["placement", "network", "occurred_at"]),
            models.Index(fields=["experiment", "variant"]),
            models.Index(fields=["occurred_at"]),
            models.Index(fields=["app", "occurred_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.event_type}:{self.event_id}"
