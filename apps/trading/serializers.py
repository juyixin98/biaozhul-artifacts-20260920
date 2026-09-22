from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from apps.trading.models import Fill, Order, Trade


class OrderCreateSerializer(serializers.Serializer):
    symbol = serializers.CharField()
    type = serializers.ChoiceField(choices=Order.OrderType.values)
    side = serializers.ChoiceField(choices=Order.Side.values)
    price = serializers.DecimalField(
        max_digits=30, decimal_places=8, required=False, allow_null=True,
        min_value=Decimal("0.00000001"),
    )
    quantity = serializers.DecimalField(
        max_digits=30, decimal_places=8, required=False, allow_null=True,
        min_value=Decimal("0.00000001"),
    )
    quote_amount = serializers.DecimalField(
        max_digits=30, decimal_places=8, required=False, allow_null=True,
        min_value=Decimal("0.00000001"),
    )
    idempotency_key = serializers.CharField(
        required=False, allow_blank=False, max_length=128
    )

    def validate(self, attrs):
        type_ = attrs["type"]
        side = attrs["side"]
        if type_ == Order.OrderType.LIMIT:
            if attrs.get("price") is None or attrs.get("quantity") is None:
                raise serializers.ValidationError(
                    "limit order requires price and quantity"
                )
            attrs.pop("quote_amount", None)
        else:
            attrs["price"] = None
            if side == Order.Side.BUY:
                if attrs.get("quote_amount") is None:
                    raise serializers.ValidationError(
                        "market BUY requires quote_amount (funds to spend)"
                    )
                attrs["quantity"] = None
            else:
                if attrs.get("quantity") is None:
                    raise serializers.ValidationError(
                        "market SELL requires quantity"
                    )
                attrs["quote_amount"] = None
        return attrs


class OrderSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="market.symbol", read_only=True)
    remaining_quantity = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            "id",
            "symbol",
            "type",
            "side",
            "price",
            "quantity",
            "quote_amount",
            "filled_quantity",
            "remaining_quantity",
            "frozen_remaining",
            "status",
            "created_at",
            "updated_at",
        )

    def get_remaining_quantity(self, obj):
        if obj.quantity is None:
            return None
        return str(obj.quantity - obj.filled_quantity)


class FillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Fill
        fields = ("role", "side", "price", "quantity", "fee")


class TradeSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="market.symbol", read_only=True)

    class Meta:
        model = Trade
        fields = (
            "id",
            "symbol",
            "taker_order",
            "maker_order",
            "price",
            "quantity",
            "taker_fee",
            "maker_fee",
            "executed_at",
        )
