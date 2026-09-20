from decimal import Decimal

from rest_framework import serializers

from .models import AuditLog, Order, TradingPair, Trade


class TradingPairSerializer(serializers.ModelSerializer):
    base = serializers.CharField(source="base.symbol", read_only=True)
    quote = serializers.CharField(source="quote.symbol", read_only=True)

    class Meta:
        model = TradingPair
        fields = [
            "symbol", "base", "quote",
            "tick_size", "lot_size", "min_notional", "is_active",
        ]


class OrderCreateSerializer(serializers.Serializer):
    pair = serializers.CharField(max_length=32)
    side = serializers.ChoiceField(choices=["BUY", "SELL"])
    type = serializers.ChoiceField(choices=["LIMIT", "MARKET"])
    quantity = serializers.DecimalField(
        max_digits=36, decimal_places=8, min_value=Decimal("0"))
    limit_price = serializers.DecimalField(
        max_digits=36, decimal_places=8, min_value=Decimal("0"),
        required=False, allow_null=True
    )
    idempotency_key = serializers.CharField(
        required=False, allow_null=True, allow_blank=False, max_length=128
    )

    def validate_quantity(self, v):
        if v <= 0:
            raise serializers.ValidationError("数量/预算必须大于 0")
        return v


class OrderSerializer(serializers.ModelSerializer):
    pair = serializers.CharField(source="pair.symbol", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id", "pair", "side", "type", "limit_price",
            "orig_qty", "filled_qty", "filled_quote",
            "remaining_frozen", "status", "created_at", "updated_at",
        ]


class TradeSerializer(serializers.ModelSerializer):
    pair = serializers.CharField(source="pair.symbol", read_only=True)

    class Meta:
        model = Trade
        fields = [
            "id", "pair", "taker_order", "maker_order",
            "price", "quantity", "quote_amount",
            "taker_fee", "maker_fee", "created_at",
        ]


class FeeConfigSerializer(serializers.Serializer):
    taker_fee_rate = serializers.DecimalField(
        max_digits=10, decimal_places=8,
        min_value=Decimal("0"), max_value=Decimal("1"), required=False
    )
    maker_fee_rate = serializers.DecimalField(
        max_digits=10, decimal_places=8,
        min_value=Decimal("0"), max_value=Decimal("1"), required=False
    )
    maintenance_mode = serializers.BooleanField(required=False)


class AuditLogSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source="actor.username", allow_null=True)

    class Meta:
        model = AuditLog
        fields = ["id", "actor", "action", "target", "detail", "created_at"]
