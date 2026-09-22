"""Application, AdNetwork and Placement models."""
import secrets

from django.conf import settings
from django.db import models


def _new_api_key() -> str:
    return secrets.token_urlsafe(32)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class App(TimeStampedModel):
    """A developer's mobile/web application."""

    developer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="apps",
    )
    name = models.CharField(max_length=255)
    bundle_id = models.CharField(
        max_length=255,
        help_text="Platform bundle / package identifier, e.g. com.acme.game",
    )
    platform = models.CharField(
        max_length=16,
        choices=[("ios", "iOS"), ("android", "Android"), ("web", "Web")],
    )
    api_key = models.CharField(
        max_length=64,
        unique=True,
        default=_new_api_key,
        editable=False,
        help_text="SDK authentication key (sent as 'Authorization: ApiKey <key>').",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["developer", "bundle_id"],
                name="uniq_app_bundle_per_developer",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.bundle_id})"


class AdNetwork(TimeStampedModel):
    """A configured ad network integration for a developer.

    Networks are defined at developer level and referenced by waterfall entries
    (at most 8 distinct networks per placement, enforced in the publish service).
    """

    developer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ad_networks",
    )
    name = models.CharField(max_length=128)
    code = models.SlugField(
        max_length=64, help_text="Stable machine code, e.g. 'adcolony'."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["developer", "code"],
                name="uniq_network_code_per_developer",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} <{self.code}>"


class Placement(TimeStampedModel):
    """An ad slot inside an App (e.g. 'level_complete_rewarded')."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="placements")
    name = models.CharField(max_length=255)
    placement_key = models.SlugField(
        max_length=128, help_text="Stable key used by the SDK to fetch config."
    )
    ad_type = models.CharField(
        max_length=32,
        choices=[
            ("banner", "Banner"),
            ("interstitial", "Interstitial"),
            ("rewarded", "Rewarded video"),
            ("native", "Native"),
        ],
        default="rewarded",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["app", "placement_key"],
                name="uniq_placement_key_per_app",
            )
        ]

    @property
    def developer_id(self):
        return self.app.developer_id

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.placement_key}@{self.app.bundle_id}"
