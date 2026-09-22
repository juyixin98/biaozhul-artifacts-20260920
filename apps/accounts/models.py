from django.db import models


class Asset(models.Model):
    """A simulated on-platform asset (e.g. BTC, ETH, USDT)."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=64, blank=True, default="")
    # Simulated minted supply, purely informational.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "assets"

    def __str__(self):
        return self.code


class Balance(models.Model):
    """One user's balance of one asset.

    ``available`` may be used for new orders/withdrawals; ``frozen`` is
    reserved by working orders.  Total = available + frozen.

    Both columns are constrained non-negative at the database level, so a
    bug that would overspend raises instead of producing a negative balance.
    """

    user = models.ForeignKey(
        "auth.User", related_name="balances", on_delete=models.PROTECT
    )
    asset = models.ForeignKey(Asset, related_name="balances", on_delete=models.PROTECT)
    available = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    frozen = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    # Equity accounts (sim-genesis) are the sole source of simulated supply
    # and are allowed to carry a negative available balance.  Trading users
    # and the fee account are always non-negative, enforced by the CHECK
    # constraints below (a table-local flag keeps the constraints valid in
    # both MySQL and sqlite).
    is_equity = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "balances"
        unique_together = ("user", "asset")
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(available__gte=0) | models.Q(is_equity=True)
                ),
                name="balance_available_nonneg",
            ),
            models.CheckConstraint(
                check=models.Q(frozen__gte=0),
                name="balance_frozen_nonneg",
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.available}+{self.frozen} {self.asset}"
