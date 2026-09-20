from django.urls import path

from . import views

urlpatterns = [
    path("pairs", views.pair_list, name="pairs"),
    path("orders", views.create_order, name="order-create"),
    path("orders/mine", views.my_orders, name="my-orders"),
    path("trades/mine", views.my_trades, name="my-trades"),
    path("orders/<int:order_id>", views.order_detail, name="order-detail"),
    path("orders/<int:order_id>/cancel", views.cancel_order, name="order-cancel"),

    # 管理端
    path("admin/config", views.fee_config_view, name="fee-config"),
    path("admin/maintenance/on", views.maintenance_on, name="maintenance-on"),
    path("admin/maintenance/off", views.maintenance_off, name="maintenance-off"),
    path("admin/audit", views.audit_log_view, name="audit-log"),
    path("admin/reconcile", views.admin_reconcile, name="admin-reconcile"),
]
