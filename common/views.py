"""Read-only audit log API scoped to the authenticated developer."""
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from common.models import AuditLog
from common.permissions import IsDeveloper


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "action",
            "resource_type",
            "resource_id",
            "resource_repr",
            "diff",
            "created_at",
        ]


class AuditLogListView(APIView):
    permission_classes = [IsDeveloper]

    def get(self, request):
        qs = AuditLog.objects.filter(developer=request.user)
        resource_type = request.query_params.get("resource_type")
        if resource_type:
            qs = qs.filter(resource_type=resource_type)
        try:
            limit = min(int(request.query_params.get("limit", 100)), 500)
        except (TypeError, ValueError):
            limit = 100
        qs = qs.order_by("-created_at", "-id")[:limit]
        return Response(AuditLogSerializer(qs, many=True).data)
