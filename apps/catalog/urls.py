from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AdNetworkViewSet,
    AppViewSet,
    AuditLogViewSet,
    ConfigVersionViewSet,
    PlacementNetworkViewSet,
    PlacementViewSet,
    SDKCurrentConfigView,
)

router = DefaultRouter()
router.register(r"apps", AppViewSet, basename="app")
router.register(r"placements", PlacementViewSet, basename="placement")
router.register(r"network-lines", PlacementNetworkViewSet, basename="networkline")
router.register(r"config-versions", ConfigVersionViewSet, basename="configversion")
router.register(r"audit-logs", AuditLogViewSet, basename="auditlog")
router.register(r"ad-networks", AdNetworkViewSet, basename="adnetwork")

urlpatterns = [
    path("", include(router.urls)),
    # SDK: fetch the live config (X-SDK-Key auth, no developer token).
    path(
        "sdk/apps/<str:app_code>/placements/<str:placement_code>/config/",
        SDKCurrentConfigView.as_view(),
        name="sdk-current-config",
    ),
]
