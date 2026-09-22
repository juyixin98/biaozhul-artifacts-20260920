from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Security-relevant operations, append-only.

    Order placement/cancellation is reconstructable from the order and
    ledger tables; this log focuses on authentication and *admin* actions
    (fee changes, maintenance toggles, market creation) plus order events
    that fail security checks (duplicate key conflicts, rejected orders).
    """

    class Action(models.TextChoices):
        REGISTER = "REGISTER", "Register"
        LOGIN = "LOGIN", "Login"
        MARKET_CREATE = "MARKET_CREATE", "Market created"
        FEE_UPDATE = "FEE_UPDATE", "Fees updated"
        MAINTENANCE_ON = "MAINTENANCE_ON", "Maintenance on"
        MAINTENANCE_OFF = "MAINTENANCE_OFF", "Maintenance off"
        ORDER_REJECTED = "ORDER_REJECTED", "Order rejected"
        IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT", "Idempotency conflict"
        DEPOSIT = "DEPOSIT", "Simulated deposit"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="audit_events",
        on_delete=models.PROTECT,
    )
    action = models.CharField(max_length=24, choices=Action.choices)
    target = models.CharField(max_length=128, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_logs"
        indexes = [
            models.Index(fields=["action", "-created_at"], name="ix_audit_action"),
            models.Index(fields=["actor", "-created_at"], name="ix_audit_actor"),
        ]
