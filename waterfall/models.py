"""Versioned waterfall configurations.

Lifecycle
---------
1. A developer builds a *draft* (``WaterfallVersion(status=draft)``). Drafts are
   freely editable; at most one draft per placement exists.
2. Publishing validates the draft (1-8 distinct networks, complete priority and
   fallback permutations, sane floors) and freezes it into an *immutable*
   version with a per-placement monotonically increasing ``version_number``.
3. The effective configuration pointer (:class:`CurrentVersion`) is swapped to
   the new version inside the same transaction -- readers either see the whole
   old version or the whole new one, never a partial mix.
4. Published versions and their entries are never modified or deleted. The SDK
   always resolves configuration through the current pointer, so historical
   versions stay available for attribution of past impressions.
"""
from django.conf import settings
from django.db import models

from applications.models import AdNetwork, Placement
from common.utils import ZERO, money_field


class WaterfallVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    placement = models.ForeignKey(
        Placement, on_delete=models.CASCADE, related_name="versions"
    )
    # Assigned at publish time; drafts keep NULL.
    version_number = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_waterfall_versions",
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_waterfall_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["placement_id", "-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["placement", "version_number"],
                name="uniq_version_number_per_placement",
            ),
        ]
        indexes = [
            models.Index(fields=["placement", "status"]),
        ]

    @property
    def is_immutable(self) -> bool:
        return self.status == self.Status.PUBLISHED

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.placement_id}/v{self.version_number or 'draft'}"


class WaterfallEntry(models.Model):
    """One network's position inside a waterfall version.

    ``priority`` expresses the commercial preference (1 = highest).
    ``fallback_order`` expresses the actual request sequence when filling an
    impression; the two may legitimately differ (e.g. a high-priority network
    is tried second because of latency), so both are explicit validated
    permutations rather than one derived from the other.
    """

    version = models.ForeignKey(
        WaterfallVersion, on_delete=models.CASCADE, related_name="entries"
    )
    network = models.ForeignKey(AdNetwork, on_delete=models.PROTECT)
    priority = models.PositiveSmallIntegerField()
    fallback_order = models.PositiveSmallIntegerField()
    floor_cpm = money_field(
        default=ZERO,
        help_text="Minimum acceptable CPM for this network in this slot.",
    )
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["version_id", "fallback_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "network"],
                name="uniq_network_per_version",
            ),
            models.UniqueConstraint(
                fields=["version", "priority"],
                name="uniq_priority_per_version",
            ),
            models.UniqueConstraint(
                fields=["version", "fallback_order"],
                name="uniq_fallback_per_version",
            ),
        ]


class CurrentVersion(models.Model):
    """The currently effective version pointer for each placement.

    Updated (never appended) when a new version is published, inside the same
    transaction that freezes the version -- this is the atomic switch.
    """

    placement = models.OneToOneField(
        Placement, on_delete=models.CASCADE, related_name="current_pointer"
    )
    version = models.ForeignKey(
        WaterfallVersion, on_delete=models.PROTECT, related_name="current_pointers"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"current({self.placement_id})=v{self.version.version_number}"
