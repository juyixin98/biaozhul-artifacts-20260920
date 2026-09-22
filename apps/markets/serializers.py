from rest_framework import serializers

from apps.accounts.models import Asset
from apps.markets.models import Market


class MarketSerializer(serializers.ModelSerializer):
    base_asset = serializers.CharField(source="base_asset.code", read_only=True)
    quote_asset = serializers.CharField(source="quote_asset.code", read_only=True)

    class Meta:
        model = Market
        fields = (
            "symbol",
            "base_asset",
            "quote_asset",
            "maker_fee_bps",
            "taker_fee_bps",
            "min_quantity",
            "min_notional",
            "is_active",
            "in_maintenance",
            "updated_at",
        )
        read_only_fields = fields


class AdminMarketSerializer(serializers.ModelSerializer):
    """Admin create / fee update."""

    base_asset = serializers.SlugRelatedField(
        slug_field="code", queryset=Asset.objects.all()
    )
    quote_asset = serializers.SlugRelatedField(
        slug_field="code", queryset=Asset.objects.all()
    )

    class Meta:
        model = Market
        fields = (
            "symbol",
            "base_asset",
            "quote_asset",
            "maker_fee_bps",
            "taker_fee_bps",
            "min_quantity",
            "min_notional",
            "is_active",
        )

    def _non_negative(self, value):
        if value < 0:
            raise serializers.ValidationError("must be non-negative")
        return value

    def _fee_cap(self, value):
        if value < 0:
            raise serializers.ValidationError("must be non-negative")
        # 10000 bps = 100%; a larger fee would consume more than received.
        if value > 10000:
            raise serializers.ValidationError("fee cannot exceed 10000 bps (100%)")
        return value

    validate_maker_fee_bps = _fee_cap
    validate_taker_fee_bps = _fee_cap
    validate_min_quantity = _non_negative
    validate_min_notional = _non_negative
