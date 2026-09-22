from rest_framework import permissions, serializers
from rest_framework.views import APIView
from rest_framework.response import Response

from apps.audit.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source="actor.username", default=None)

    class Meta:
        model = AuditLog
        fields = ("id", "actor", "action", "target", "detail",
                  "ip_address", "created_at")


class IsAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff)


class AdminAuditLogView(APIView):
    """Admin-only audit trail.  Supports ?action=&actor=&limit=&offset=."""

    permission_classes = (permissions.IsAuthenticated, IsAdmin)

    def get(self, request):
        qs = AuditLog.objects.select_related("actor").order_by("-id")
        action = request.query_params.get("action")
        if action:
            qs = qs.filter(action=action.upper())
        actor = request.query_params.get("actor")
        if actor:
            qs = qs.filter(actor__username=actor)
        target = request.query_params.get("target")
        if target:
            qs = qs.filter(target=target)
        limit = min(int(request.query_params.get("limit", 50)), 500)
        offset = int(request.query_params.get("offset", 0))
        total = qs.count()
        rows = qs[offset: offset + limit]
        return Response(
            {"count": total, "results": AuditLogSerializer(rows, many=True).data}
        )
