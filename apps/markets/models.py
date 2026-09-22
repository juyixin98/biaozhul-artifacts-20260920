from decimal import Decimal

from django.db import models


class Market(models.Model):
    """A trading pair BASE/QUOTE, e.g. BTC/USDT.

    The Market row itself is the engine's per-pair lock: order submission,
    matching and cancellation take ``SELECT ... FOR UPDATE`` on it, so all
    operations on one pair are serialised while different pairs run in
    parallel.
    """

    symbol = models.CharField(max_length=32, unique=True)
    base_asset = models.ForeignKey(
        "accounts.Asset", related_name="base_markets", on_delete=models.PROTECT
    )
    quote_asset = models.ForeignKey(
        "accounts.Asset", related_name="quote_markets", on_delete=models.PROTECT
    )

    # Fees in basis points.  10 bps = 0.10%.
    maker_fee_bps = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    taker_fee_bps = models.DecimalField(max_digits=10, decimal_places=4, default=10)

    min_quantity = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    min_notional = models.DecimalField(max_digits=30, decimal_places=8, default=0)

    is_active = models.BooleanField(default=True)
    # Maintenance mode: new orders are rejected; entering maintenance cancels
    # every resting order and releases its frozen balance.
    in_maintenance = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "markets"

    def __str__(self):
        return self.symbol

    def save(self, *args, **kwargs):
        if not self.symbol:
            self.symbol = f"{self.base_asset.code}-{self.quote_asset.code}"
        super().save(*args, **kwargs)

    @property
    def maker_fee_rate(self) -> Decimal:
        return Decimal(self.maker_fee_bps) / Decimal(10000)

    @property
    def taker_fee_rate(self) -> Decimal:
        return Decimal(self.taker_fee_bps) / Decimal(10000)
