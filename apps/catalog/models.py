"""Catalog models: developers' apps, placements, waterfall draft lines,
immutable published configuration versions and the append-only audit log.

Money convention (see apps.common.money): every monetary column is
DECIMAL(20,6) and values are quantized to 6 fractional places before write.
Floats are rejected at the serializer boundary.
"""
import uuid

from django.conf import settings
from django.db import models


class ImmutableModel(models.Model):
    """Insert-only base: once a row exists it can never be mutated.

    The ``_immutable_forbidden_fields`` escape hatch is reserved for internal
    maintenance and is never exposed through the API.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if kwargs.pop("_allow_mutation", False):
            return super().save(*args, **kwargs)
        if self.pk is not None:
            raise models.ProtectedError(
                f"{self.__class__.__name__} rows are immutable once created",
                [self],
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise models.ProtectedError(
            f"{self.__class__.__name__} rows are immutable and cannot be deleted",
            [self],
        )


class App(models.Model):
    """A developer's integrated application."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="apps",
    )
    name = models.CharField(max_length=120)
    code = models.SlugField(max_length=64)
    # SDK authenticates with this key; rotated by regenerating the app.
    sdk_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "code"], name="uniq_app_code_per_owner"
            )
        ]
        ordering = ["id"]

    def __str__(self):
        return f"{self.code} ({self.owner})"


class AdNetwork(models.Model):
    """Global catalog of known ad networks (no external calls ever made)."""

    code = models.SlugField(max_length=64, unique=True)
    display_name = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class Placement(models.Model):
    """An ad placement inside an app; owns one editable waterfall draft."""

    class Format(models.TextChoices):
        BANNER = "banner", "Banner"
        INTERSTITIAL = "interstitial", "Interstitial"
        REWARDED = "rewarded", "Rewarded"
        NATIVE = "native", "Native"

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="placements")
    name = models.CharField(max_length=120)
    code = models.SlugField(max_length=64)
    format = models.CharField(
        max_length=20, choices=Format.choices, default=Format.INTERSTITIAL
    )
    # Points at the currently live immutable version; NULL until first publish.
    # Swapped atomically inside the publish transaction.
    active_version = models.ForeignKey(
        "ConfigVersion",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["app", "code"], name="uniq_placement_code_per_app"
            )
        ]
        ordering = ["id"]

    def __str__(self):
        return f"{self.app.code}/{self.code}"


class PlacementNetwork(models.Model):
    """One editable waterfall line (the draft that gets published)."""

    placement = models.ForeignKey(
        Placement, on_delete=models.CASCADE, related_name="network_lines"
    )
    network = models.ForeignKey(
        AdNetwork, on_delete=models.PROTECT, related_name="placement_lines"
    )
    # 1 = tried first in the waterfall.
    priority = models.PositiveSmallIntegerField()
    cpm_floor = models.DecimalField(max_digits=20, decimal_places=6)
    # Fallback chain position; independent of priority so ops can encode
    # a distinct fallback order.
    fallback_order = models.PositiveSmallIntegerField()
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["placement", "network"], name="uniq_network_per_placement"
            ),
            models.UniqueConstraint(
                fields=["placement", "priority"],
                condition=models.Q(enabled=True),
                name="uniq_priority_per_placement",
            ),
        ]
        ordering = ["placement_id", "priority"]


class ConfigVersion(ImmutableModel):
    """An immutable, published waterfall configuration snapshot."""

    class Status(models.TextChoices):
        PUBLISHED = "published", "Published"
        ACTIVE = "active", "Active"
        # Superseded versions keep status=published; Placement.active_version is
        # the single source of truth for which one is live.

    placement = models.ForeignKey(
        Placement, on_delete=models.PROTECT, related_name="versions"
    )
    version = models.PositiveIntegerField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PUBLISHED
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="published_versions",
    )
    published_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["placement", "version"], name="uniq_version_per_placement"
            )
        ]
        ordering = ["placement_id", "-version"]


class ConfigVersionEntry(ImmutableModel):
    """Immutable snapshot row belonging to a ConfigVersion."""

    version = models.ForeignKey(
        ConfigVersion, on_delete=models.PROTECT, related_name="entries"
    )
    network = models.ForeignKey(
        AdNetwork, on_delete=models.PROTECT, related_name="version_entries"
    )
    # Network identity is snapshotted in string form too, so catalog renames
    # never alter an old version.
    network_code = models.SlugField(max_length=64)
    network_name = models.CharField(max_length=120)
    priority = models.PositiveSmallIntegerField()
    cpm_floor = models.DecimalField(max_digits=20, decimal_places=6)
    fallback_order = models.PositiveSmallIntegerField()
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["priority", "network_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "network"], name="uniq_network_per_version"
            )
        ]


class AuditLog(ImmutableModel):
    """Append-only audit trail of configuration-affecting actions."""

    class Action(models.TextChoices):
        APP_CREATE = "app.create", "App created"
        PLACEMENT_CREATE = "placement.create", "Placement created"
        PLACEMENT_UPDATE = "placement.update", "Placement updated"
        NETWORK_ADD = "network.add", "Network line added"
        NETWORK_UPDATE = "network.update", "Network line updated"
        NETWORK_REMOVE = "network.remove", "Network line removed"
        CONFIG_PUBLISH = "config.publish", "Configuration published"
        EXPERIMENT_CREATE = "experiment.create", "Experiment created"
        EXPERIMENT_STOP = "experiment.stop", "Experiment stopped"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="audit_logs",
    )
    app = models.ForeignKey(
        App, on_delete=models.CASCADE, related_name="audit_logs"
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    target_type = models.CharField(max_length=64)
    target_id = models.CharField(max_length=64, blank=True, default="")
    target_repr = models.CharField(max_length=255, blank=True, default="")
    # Structured before/after payload. Never contains secrets.
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["app", "action"])]
