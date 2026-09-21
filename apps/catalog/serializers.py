from decimal import Decimal

from rest_framework import serializers

from apps.common.money import quantize_money
from .models import (
    AdNetwork,
    App,
    AuditLog,
    ConfigVersion,
    ConfigVersionEntry,
    Placement,
    PlacementNetwork,
)


class AdNetworkSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdNetwork
        fields = ("id", "code", "display_name", "is_active")
        read_only_fields = ("id",)


class AppSerializer(serializers.ModelSerializer):
    sdk_key = serializers.UUIDField(read_only=True)
    owner = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = App
        fields = ("id", "owner", "name", "code", "sdk_key", "created_at")
        read_only_fields = ("id", "owner", "sdk_key", "created_at")


class PlacementSerializer(serializers.ModelSerializer):
    active_version_number = serializers.SerializerMethodField()

    class Meta:
        model = Placement
        fields = (
            "id",
            "app",
            "name",
            "code",
            "format",
            "active_version",
            "active_version_number",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "active_version",
            "active_version_number",
            "created_at",
            "updated_at",
        )

    def get_active_version_number(self, obj):
        return obj.active_version.version if obj.active_version else None

    def validate_app(self, app):
        request = self.context["request"]
        if app.owner_id != request.user.id:
            raise serializers.ValidationError("App not found or not owned by you.")
        return app


class MoneyField(serializers.DecimalField):
    """DecimalField that accepts strings/numbers but never python floats
    unquantized; output is always a fixed 6-dp string."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 20)
        kwargs.setdefault("decimal_places", 6)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if isinstance(data, float):
            self.fail("invalid")
        try:
            quantized = quantize_money(data)
        except Exception:
            self.fail("invalid")
        if quantized < Decimal("0"):
            self.fail("invalid")
        return quantized


class PlacementNetworkSerializer(serializers.ModelSerializer):
    cpm_floor = MoneyField()

    class Meta:
        model = PlacementNetwork
        fields = (
            "id",
            "placement",
            "network",
            "priority",
            "cpm_floor",
            "fallback_order",
            "enabled",
            "updated_at",
        )
        read_only_fields = ("id", "updated_at")

    def _placement_for(self, attrs):
        if self.instance is not None:
            return self.instance.placement
        return attrs.get("placement")

    def validate(self, attrs):
        request = self.context["request"]
        placement = self._placement_for(attrs)
        if placement is None:
            raise serializers.ValidationError({"placement": "This field is required."})
        if placement.app.owner_id != request.user.id:
            raise serializers.ValidationError("Placement not found or not owned by you.")

        network = attrs.get("network") or getattr(self.instance, "network", None)
        if network is not None and not network.is_active:
            raise serializers.ValidationError(
                {"network": f"Ad network '{network.code}' is not active."}
            )

        priority = attrs.get("priority", getattr(self.instance, "priority", None))
        fallback = attrs.get(
            "fallback_order", getattr(self.instance, "fallback_order", None)
        )
        if priority is not None and priority < 1:
            raise serializers.ValidationError({"priority": "Must be >= 1."})
        if fallback is not None and fallback < 1:
            raise serializers.ValidationError(
                {"fallback_order": "Must be >= 1."}
            )

        max_networks = 8
        from django.conf import settings as dj_settings

        max_networks = getattr(dj_settings, "MAX_NETWORKS_PER_PLACEMENT", 8)
        existing_count = PlacementNetwork.objects.filter(placement=placement).count()
        adding = self.instance is None
        if adding and existing_count >= max_networks:
            raise serializers.ValidationError(
                f"A placement may have at most {max_networks} ad networks."
            )
        return attrs


class ConfigVersionEntrySerializer(serializers.ModelSerializer):
    cpm_floor = serializers.DecimalField(max_digits=20, decimal_places=6)

    class Meta:
        model = ConfigVersionEntry
        fields = (
            "network_code",
            "network_name",
            "priority",
            "cpm_floor",
            "fallback_order",
            "enabled",
        )


class ConfigVersionSerializer(serializers.ModelSerializer):
    entries = ConfigVersionEntrySerializer(many=True, read_only=True)
    published_by = serializers.SlugRelatedField(slug_field="username", read_only=True)

    class Meta:
        model = ConfigVersion
        fields = (
            "id",
            "placement",
            "version",
            "status",
            "note",
            "published_by",
            "published_at",
            "entries",
        )


class AuditLogSerializer(serializers.ModelSerializer):
    actor = serializers.SlugRelatedField(slug_field="username", read_only=True)

    class Meta:
        model = AuditLog
        fields = (
            "id",
            "actor",
            "app",
            "action",
            "target_type",
            "target_id",
            "target_repr",
            "payload",
            "created_at",
        )
