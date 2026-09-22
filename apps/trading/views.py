from decimal import InvalidOperation, Decimal

from django.db.models import Q
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import record
from apps.markets.models import Market
from apps.markets.serializers import AdminMarketSerializer, MarketSerializer
from apps.trading import engine
from apps.trading.book_registry import registry
from apps.trading.models import Order, Trade
from apps.trading.retry import is_lock_error, retry_on_lock
from apps.trading.serializers import (
    OrderCreateSerializer,
    OrderSerializer,
    TradeSerializer,
)


class IsAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff)


class OrderListCreateView(APIView):
    def get(self, request):
        """List the caller's orders (never anyone else's)."""
        qs = (
            Order.objects.filter(user=request.user)
            .select_related("market")
            .order_by("-id")
        )
        symbol = request.query_params.get("symbol")
        if symbol:
            qs = qs.filter(market__symbol=symbol.upper())
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        limit = int(request.query_params.get("limit", 50))
        offset = int(request.query_params.get("offset", 0))
        total = qs.count()
        rows = qs[offset: offset + min(limit, 200)]
        return Response(
            {
                "count": total,
                "results": OrderSerializer(rows, many=True).data,
            }
        )

    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            market = Market.objects.select_related(
                "base_asset", "quote_asset"
            ).get(symbol=data["symbol"].upper())
        except Market.DoesNotExist:
            return Response(
                {"detail": f"unknown symbol {data['symbol']}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        idem = data.get("idempotency_key")
        # Hash the *business* parameters only.
        payload = {
            "symbol": market.symbol,
            "type": data["type"],
            "side": data["side"],
            "price": str(data["price"]) if data.get("price") is not None else None,
            "quantity": str(data["quantity"])
            if data.get("quantity") is not None
            else None,
            "quote_amount": str(data["quote_amount"])
            if data.get("quote_amount") is not None
            else None,
        }
        try:
            report = retry_on_lock(lambda: engine.submit_order(
                user=request.user,
                market=market,
                type=data["type"],
                side=data["side"],
                price=data.get("price"),
                quantity=data.get("quantity"),
                quote_amount=data.get("quote_amount"),
                idempotency_key=idem,
                request_payload=payload,
                ip_address=_ip(request),
            ))
        except engine.Conflict as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except engine.OrderRejected as exc:
            record(
                "ORDER_REJECTED",
                actor=request.user,
                target=market.symbol,
                detail={"reason": str(exc), "payload": payload},
                ip_address=_ip(request),
            )
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )

        # Structural cache update only after the commit has returned: keeps
        # the in-memory book identical to persisted state.
        order = report.order
        order.refresh_from_db()
        if not report.replayed:
            engine.apply_book_changes(market.id, report)
        http_status = status.HTTP_200_OK if report.replayed else status.HTTP_201_CREATED
        return Response(
            {
                "order": OrderSerializer(order).data,
                "trades": TradeSerializer(report.trades, many=True).data,
                "replayed": report.replayed,
            },
            status=http_status,
        )


class OrderDetailView(APIView):
    def _get_owned(self, request, order_id):
        return Order.objects.filter(id=order_id, user=request.user).first()

    def get(self, request, order_id):
        order = self._get_owned(request, order_id)
        if order is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(OrderSerializer(order).data)

    def delete(self, request, order_id):
        order = self._get_owned(request, order_id)
        if order is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            canceled = retry_on_lock(lambda: engine.cancel_order(
                user=request.user, order_id=order.id
            ))
        except engine.OrderNotCancelable as exc:
            order.refresh_from_db()
            return Response(
                {"detail": str(exc), "status": order.status},
                status=status.HTTP_409_CONFLICT,
            )
        engine.sync_book_after_commit(
            order.market_id, order=canceled, canceled=True
        )
        canceled.refresh_from_db()
        return Response(OrderSerializer(canceled).data)


class MyTradesView(APIView):
    def get(self, request):
        qs = Trade.objects.filter(
            Q(taker_order__user=request.user)
            | Q(maker_order__user=request.user)
        ).order_by("-id")
        symbol = request.query_params.get("symbol")
        if symbol:
            qs = qs.filter(market__symbol=symbol.upper())
        limit = int(request.query_params.get("limit", 50))
        offset = int(request.query_params.get("offset", 0))
        total = qs.count()
        rows = qs[offset: offset + min(limit, 200)]
        return Response(
            {"count": total, "results": TradeSerializer(rows, many=True).data}
        )


class OrderBookView(APIView):
    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    def get(self, request, symbol):
        try:
            market = Market.objects.get(symbol=symbol.upper())
        except Market.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        depth = min(int(request.query_params.get("depth", 50)), 500)
        book = registry.get(market.id)
        data = book.snapshot(depth=depth)
        data["symbol"] = market.symbol
        data["in_maintenance"] = market.in_maintenance
        return Response(data)


class MarketListView(generics.ListAPIView):
    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()
    serializer_class = MarketSerializer

    def get_queryset(self):
        return Market.objects.filter(is_active=True).order_by("symbol")


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


class AdminMarketCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAdmin)

    def post(self, request):
        ser = AdminMarketSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        market = ser.save()
        registry.invalidate(market.id)
        record(
            "MARKET_CREATE",
            actor=request.user,
            target=market.symbol,
            detail=AdminMarketSerializer(market).data,
            ip_address=_ip(request),
        )
        return Response(
            MarketSerializer(market).data, status=status.HTTP_201_CREATED
        )


class AdminFeeUpdateView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAdmin)

    def patch(self, request, symbol):
        try:
            market = Market.objects.get(symbol=symbol.upper())
        except Market.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        before = {
            "maker_fee_bps": str(market.maker_fee_bps),
            "taker_fee_bps": str(market.taker_fee_bps),
        }
        changed = False
        for field_name in ("maker_fee_bps", "taker_fee_bps"):
            if field_name in request.data:
                try:
                    value = Decimal(str(request.data[field_name]))
                except InvalidOperation:
                    return Response(
                        {field_name: "not a number"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if value < 0 or value > Decimal("10000"):
                    return Response(
                        {field_name: "must be between 0 and 10000 bps"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                setattr(market, field_name, value)
                changed = True
        if changed:
            market.save(update_fields=["maker_fee_bps", "taker_fee_bps",
                                       "updated_at"])
        record(
            "FEE_UPDATE",
            actor=request.user,
            target=market.symbol,
            detail={"before": before, "after": {
                "maker_fee_bps": str(market.maker_fee_bps),
                "taker_fee_bps": str(market.taker_fee_bps),
            }},
            ip_address=_ip(request),
        )
        return Response(MarketSerializer(market).data)


class AdminMaintenanceView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAdmin)

    def post(self, request, symbol):
        try:
            market = Market.objects.get(symbol=symbol.upper())
        except Market.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        enabled = bool(request.data.get("enabled", True))
        market, canceled_ids = retry_on_lock(lambda: engine.set_maintenance(
            admin_user=request.user,
            market_id=market.id,
            enabled=enabled,
            ip_address=_ip(request),
        ))
        if enabled:
            registry.invalidate(market.id)
        return Response(
            {
                "symbol": market.symbol,
                "in_maintenance": market.in_maintenance,
                "canceled_order_count": len(canceled_ids),
                "canceled_order_ids": canceled_ids,
            }
        )


def _ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")
