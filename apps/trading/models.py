from decimal import Decimal

from django.conf import settings
from django.db import models


class Order(models.Model):
    class OrderType(models.TextChoices):
        LIMIT = "LIMIT", "Limit"
        MARKET = "MARKET", "Market"

    class Side(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    class Status(models.TextChoices):
        NEW = "NEW", "New"                       # resting, unfilled
        PARTIALLY_FILLED = "PARTIAL", "Partially filled"  # resting
        FILLED = "FILLED", "Filled"             # terminal
        CANCELED = "CANCELED", "Canceled"       # terminal (may be partially filled)
        REJECTED = "REJECTED", "Rejected"       # terminal

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="orders", on_delete=models.PROTECT
    )
    market = models.ForeignKey(
        "markets.Market", related_name="orders", on_delete=models.PROTECT
    )
    type = models.CharField(max_length=8, choices=OrderType.choices)
    side = models.CharField(max_length=4, choices=Side.choices)

    # NULL for market BUY (quote-funded) -- see quote_amount.
    price = models.DecimalField(
        max_digits=30, decimal_places=8, null=True, blank=True
    )
    # Base quantity requested; NULL for market BUY.
    quantity = models.DecimalField(
        max_digits=30, decimal_places=8, null=True, blank=True
    )
    # Quote amount to spend, only for market BUY.
    quote_amount = models.DecimalField(
        max_digits=30, decimal_places=8, null=True, blank=True
    )

    filled_quantity = models.DecimalField(
        max_digits=30, decimal_places=8, default=Decimal("0")
    )
    # Remaining frozen asset backing the resting quantity (base for sells,
    # quote for buys incl. market buy reserve).  Released on fill/cancel.
    frozen_remaining = models.DecimalField(
        max_digits=30, decimal_places=8, default=Decimal("0")
    )
    quote_filled = models.DecimalField(
        max_digits=30, decimal_places=8, default=Decimal("0")
    )

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.NEW, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Monotonic sequence used as the time-priority tie-breaker inside a price
    # level; assigned from the order's auto-increment id (strictly increasing
    # with created_at) so a recovered book keeps identical price-time order.
    sequence = models.BigIntegerField(default=0, db_index=True)

    class Meta:
        db_table = "orders"
        indexes = [
            # Resting-order recovery / book build.
            models.Index(
                fields=["market", "side", "status", "price", "sequence"],
                name="ix_order_book",
            ),
            models.Index(fields=["user", "-created_at"], name="ix_order_user"),
        ]

    def __str__(self):
        return f"#{self.id} {self.type} {self.side} {self.market_id} {self.status}"

    @property
    def is_working(self) -> bool:
        return self.status in (self.Status.NEW, self.Status.PARTIALLY_FILLED)

    @property
    def remaining_quantity(self) -> Decimal:
        if self.quantity is None:
            return Decimal("0")
        return self.quantity - self.filled_quantity


class Trade(models.Model):
    """One match between a taker order and a resting maker order."""

    market = models.ForeignKey(
        "markets.Market", related_name="trades", on_delete=models.PROTECT
    )
    taker_order = models.ForeignKey(
        Order, related_name="taker_trades", on_delete=models.PROTECT
    )
    maker_order = models.ForeignKey(
        Order, related_name="maker_trades", on_delete=models.PROTECT
    )
    price = models.DecimalField(max_digits=30, decimal_places=8)
    quantity = models.DecimalField(max_digits=30, decimal_places=8)
    taker_fee = models.DecimalField(max_digits=30, decimal_places=8)
    maker_fee = models.DecimalField(max_digits=30, decimal_places=8)
    executed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "trades"
        indexes = [
            models.Index(fields=["market", "-executed_at"], name="ix_trade_mkt"),
        ]

    def __str__(self):
        return (
            f"{self.quantity}@{self.price} "
            f"(taker #{self.taker_order_id}, maker #{self.maker_order_id})"
        )


class Fill(models.Model):
    """One side of a Trade -- an append-only per-order fill record.

    The unique constraints make double-settlement impossible: after a crash
    the recovery/reconciliation logic can never insert the same (order,
    trade, side) twice, and ``quantity``/``fee`` on the order are updated in
    the same transaction that inserts these rows.
    """

    class Role(models.TextChoices):
        TAKER = "TAKER", "Taker"
        MAKER = "MAKER", "Maker"

    trade = models.ForeignKey(
        Trade, related_name="fills", on_delete=models.PROTECT
    )
    order = models.ForeignKey(Order, related_name="fills", on_delete=models.PROTECT)
    role = models.CharField(max_length=5, choices=Role.choices)
    side = models.CharField(max_length=4, choices=Order.Side.choices)
    price = models.DecimalField(max_digits=30, decimal_places=8)
    quantity = models.DecimalField(max_digits=30, decimal_places=8)
    fee = models.DecimalField(max_digits=30, decimal_places=8)

    class Meta:
        db_table = "fills"
        constraints = [
            models.UniqueConstraint(
                fields=["trade", "order"], name="uq_fill_trade_order"
            ),
        ]


class IdempotencyKey(models.Model):
    """Client-supplied idempotency key for order placement.

    Repeating the same key returns the original order.  Reusing the key with
    a different request body is a 409 conflict.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="idempotency_keys",
        on_delete=models.CASCADE,
    )
    key = models.CharField(max_length=128)
    order = models.ForeignKey(
        Order, related_name="idempotency_keys", on_delete=models.PROTECT
    )
    request_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "idempotency_keys"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "key"], name="uq_idem_user_key"
            ),
        ]
