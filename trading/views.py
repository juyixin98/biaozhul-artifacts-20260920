"""交易 API 视图。"""
from django.db import transaction as db_transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from accounts.reconciliation import reconcile_snapshot

from .engine import _audit, engine
from .exceptions import (
    IdempotencyConflict,
    MaintenanceActive,
    OrderNotOpen,
    OrderNotFound,
)
from .models import AuditLog, Order, SystemConfig, TradingPair
from .serializers import (
    AuditLogSerializer,
    FeeConfigSerializer,
    OrderCreateSerializer,
    OrderSerializer,
    TradeSerializer,
    TradingPairSerializer,
)


@api_view(["GET"])
def pair_list(request):
    qs = TradingPair.objects.filter(is_active=True).select_related("base", "quote")
    return Response(TradingPairSerializer(qs, many=True).data)


@api_view(["POST"])
def create_order(request):
    ser = OrderCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    pair = TradingPair.objects.filter(symbol=data["pair"], is_active=True).first()
    if pair is None:
        raise ValidationError({"pair": "交易对不存在或已停用"})

    try:
        order, created = engine.submit_order(
            user=request.user,
            pair=pair,
            side=data["side"],
            order_type=data["type"],
            quantity=data["quantity"],
            limit_price=data.get("limit_price"),
            idem_key=data.get("idempotency_key"),
        )
    except MaintenanceActive:
        return Response(
            {"detail": "系统维护中，已暂停接单"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except IdempotencyConflict as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    except ValueError as exc:
        raise ValidationError({"detail": str(exc)})

    _audit(request.user, "ORDER_SUBMIT", f"order:{order.pk}",
           f"{data['side']} {data['type']} {data['pair']} created={created}")
    code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return Response(OrderSerializer(order).data, status=code)


@api_view(["POST"])
def cancel_order(request, order_id: int):
    try:
        order = engine.cancel_order(user=request.user, order_id=order_id)
    except OrderNotFound as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except OrderNotOpen as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    return Response(OrderSerializer(order).data)


@api_view(["GET"])
def my_orders(request):
    qs = Order.objects.filter(user=request.user).select_related("pair").order_by(
        "-id"
    )[:200]
    return Response(OrderSerializer(qs, many=True).data)


@api_view(["GET"])
def order_detail(request, order_id: int):
    order = Order.objects.select_related("pair").filter(pk=order_id).first()
    if order is None or order.user_id != request.user.id:
        return Response({"detail": "订单不存在"}, status=status.HTTP_404_NOT_FOUND)
    return Response(OrderSerializer(order).data)


@api_view(["GET"])
def my_trades(request):
    """只暴露当前用户参与（taker 或 maker）的成交。"""
    from .models import Trade

    taker_ids = Trade.objects.filter(taker_order__user=request.user)
    maker_ids = Trade.objects.filter(maker_order__user=request.user)
    qs = taker_ids.union(maker_ids).order_by("-id")[:200]
    return Response(TradeSerializer(qs, many=True).data)


# ---------------- 管理端 ----------------
@api_view(["GET", "PATCH"])
@permission_classes([IsAdminUser])
def fee_config_view(request):
    cfg = SystemConfig.load()
    if request.method == "GET":
        return Response(
            {
                "taker_fee_rate": str(cfg.taker_fee_rate),
                "maker_fee_rate": str(cfg.maker_fee_rate),
                "maintenance_mode": cfg.maintenance_mode,
            }
        )

    ser = FeeConfigSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    with db_transaction.atomic():
        cfg = SystemConfig.objects.select_for_update().get(pk=1)
        changes = []
        for field in ("taker_fee_rate", "maker_fee_rate"):
            if field in ser.validated_data:
                changes.append(f"{field}={getattr(cfg, field)}->"
                               f"{ser.validated_data[field]}")
                setattr(cfg, field, ser.validated_data[field])
        cfg.save()
    _audit(request.user, "FEE_CONFIG_UPDATE", "system", "; ".join(changes) or "无变更")
    return Response(
        {
            "taker_fee_rate": str(cfg.taker_fee_rate),
            "maker_fee_rate": str(cfg.maker_fee_rate),
            "maintenance_mode": cfg.maintenance_mode,
        }
    )


@api_view(["POST"])
@permission_classes([IsAdminUser])
def maintenance_on(request):
    canceled = engine.enter_maintenance(actor=request.user)
    return Response({"maintenance_mode": True, "canceled_orders": canceled})


@api_view(["POST"])
@permission_classes([IsAdminUser])
def maintenance_off(request):
    engine.exit_maintenance(actor=request.user)
    return Response({"maintenance_mode": False})


@api_view(["GET"])
@permission_classes([IsAdminUser])
def audit_log_view(request):
    qs = AuditLog.objects.select_related("actor").order_by("-id")[:500]
    return Response(AuditLogSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAdminUser])
def admin_reconcile(request):
    return Response(reconcile_snapshot())
