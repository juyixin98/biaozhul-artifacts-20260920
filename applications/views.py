"""Tenant-scoped viewsets for App / AdNetwork / Placement."""
from rest_framework import viewsets

from common.permissions import IsDeveloper

from .models import AdNetwork, App, Placement
from .serializers import AdNetworkSerializer, AppSerializer, PlacementSerializer


class AppViewSet(viewsets.ModelViewSet):
    serializer_class = AppSerializer
    permission_classes = [IsDeveloper]

    def get_queryset(self):
        return App.objects.filter(developer=self.request.user)

    def perform_destroy(self, instance):
        from common.audit import record_audit

        record_audit(
            developer=self.request.user,
            action="delete",
            resource_type="app",
            resource_id=instance.id,
            resource_repr=instance.name,
            diff={"bundle_id": instance.bundle_id},
        )
        instance.delete()


class AdNetworkViewSet(viewsets.ModelViewSet):
    serializer_class = AdNetworkSerializer
    permission_classes = [IsDeveloper]

    def get_queryset(self):
        return AdNetwork.objects.filter(developer=self.request.user)

    def perform_destroy(self, instance):
        from common.audit import record_audit

        record_audit(
            developer=self.request.user,
            action="delete",
            resource_type="ad_network",
            resource_id=instance.id,
            resource_repr=instance.name,
            diff={"code": instance.code},
        )
        instance.delete()


class PlacementViewSet(viewsets.ModelViewSet):
    serializer_class = PlacementSerializer
    permission_classes = [IsDeveloper]

    def get_queryset(self):
        return Placement.objects.filter(app__developer=self.request.user).select_related(
            "app"
        )

    def perform_destroy(self, instance):
        from common.audit import record_audit

        record_audit(
            developer=self.request.user,
            action="delete",
            resource_type="placement",
            resource_id=instance.id,
            resource_repr=instance.placement_key,
            diff={"app_id": instance.app_id},
        )
        instance.delete()
