from django.urls import path

from apps.trading.views import (
    AdminFeeUpdateView,
    AdminMaintenanceView,
    AdminMarketCreateView,
    MarketListView,
    MyTradesView,
    OrderBookView,
    OrderDetailView,
    OrderListCreateView,
)

urlpatterns = [
    path("markets/", MarketListView.as_view(), name="market-list"),
    path("markets/<str:symbol>/orderbook/", OrderBookView.as_view(),
         name="orderbook"),
    path("orders/", OrderListCreateView.as_view(), name="order-list-create"),
    path("orders/<int:order_id>/", OrderDetailView.as_view(), name="order-detail"),
    path("trades/", MyTradesView.as_view(), name="my-trades"),

    # Admin
    path("admin/markets/", AdminMarketCreateView.as_view(),
         name="admin-market-create"),
    path("admin/markets/<str:symbol>/fees/", AdminFeeUpdateView.as_view(),
         name="admin-fee-update"),
    path("admin/markets/<str:symbol>/maintenance/", AdminMaintenanceView.as_view(),
         name="admin-maintenance"),
]
