from django.conf import settings
from django.db import models


class LedgerEvent(models.Model):
    """A group of balanced double entries recording one balance movement.

    Every balance change in the engine (freeze, release, fill settlement,
    simulated deposit) creates exactly one event whose entries sum to zero
    per asset.  Events are append-only: there is no update or delete code
    path, and reconciliation replays them against the current balances.
    """

    class EventType(models.TextChoices):
        FREEZE = "FREEZE", "Freeze on new order"
        RELEASE = "RELEASE", "Release on cancel / leftover"
        TRADE = "TRADE", "Trade settlement"
        DEPOSIT = "DEPOSIT", "Simulated deposit"
        WITHDRAWAL = "WITHDRAWAL", "Simulated withdrawal"
        MAINTENANCE = "MAINTENANCE", "Maintenance bulk release"

    type = models.CharField(max_length=16, choices=EventType.choices)
    # Generic back-reference: order id or trade id depending on type.
    order = models.ForeignKey(
        "trading.Order",
        null=True,
        blank=True,
        related_name="ledger_events",
        on_delete=models.PROTECT,
    )
    trade = models.ForeignKey(
        "trading.Trade",
        null=True,
        blank=True,
        related_name="ledger_events",
        on_delete=models.PROTECT,
    )
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ledger_events"


class LedgerEntry(models.Model):
    """One signed balance movement.

    Signed amount convention: positive = credit into ``user``'s balance in
    ``subaccount``; negative = debit out.  Entries of one event sum to zero
    per asset, which the CHECK plus application-level construction enforce.
    """

    class SubAccount(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        FROZEN = "FROZEN", "Frozen"

    event = models.ForeignKey(
        LedgerEvent, related_name="entries", on_delete=models.PROTECT
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="ledger_entries",
        on_delete=models.PROTECT,
    )
    asset = models.ForeignKey(
        "accounts.Asset", related_name="ledger_entries", on_delete=models.PROTECT
    )
    subaccount = models.CharField(max_length=10, choices=SubAccount.choices)
    amount = models.DecimalField(max_digits=30, decimal_places=8)
    # Balance after applying the entry, recorded for audit trail.
    balance_after = models.DecimalField(max_digits=30, decimal_places=8)

    class Meta:
        db_table = "ledger_entries"
        indexes = [
            models.Index(
                fields=["user", "asset", "-id"], name="ix_ledger_user_asset"
            ),
        ]
