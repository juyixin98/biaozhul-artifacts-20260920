"""Aggregated v1 URL configuration."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounts.views import DeveloperObtainTokenView, RegisterView
from applications.views import AdNetworkViewSet, AppViewSet, PlacementViewSet
from common.views import AuditLogListView
from waterfall.sdk_views import SdkConfigView
from waterfall.views import (
    CurrentConfigView,
    DraftView,
    PublishView,
    VersionListView,
)

router = DefaultRouter()
router.register("apps", AppViewSet, basename="app")
router.register("networks", AdNetworkViewSet, basename="network")
router.register("placements", PlacementViewSet, basename="placement")

urlpatterns = [
    # Auth
    path("auth/register/", RegisterView.as_view(), name="auth-register"),
    path("auth/api-token/", DeveloperObtainTokenView.as_view(), name="auth-token"),

    # Management CRUD
    path("", include(router.urls)),

    # Waterfall lifecycle
    path(
        "placements/<int:placement_pk>/versions/",
        VersionListView.as_view(),
        name="placement-versions",
    ),
    path(
        "placements/<int:placement_pk>/config/",
        CurrentConfigView.as_view(),
        name="placement-current-config",
    ),
    path(
        "placements/<int:placement_pk>/draft/",
        DraftView.as_view(),
        name="placement-draft",
    ),
    path(
        "placements/<int:placement_pk>/publish/",
        PublishView.as_view(),
        name="placement-publish",
    ),

    # Experiments
    path("", include("experiments.urls")),

    # Scores
    path("", include("scoring.urls")),

    # Audit
    path("audit-logs/", AuditLogListView.as_view(), name="audit-logs"),

    # SDK / ingestion
    path("sdk/config/", SdkConfigView.as_view(), name="sdk-config"),
    path("", include("events.urls")),
]
