from django.urls import path

from apps.audit.views import AdminAuditLogView

urlpatterns = [
    path("admin/audit-logs/", AdminAuditLogView.as_view(), name="admin-audit-logs"),
]
