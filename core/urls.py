"""core 路由。"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ConflictViewSet,
    FormRecordViewSet,
    FormTemplateViewSet,
    MeView,
    ProjectViewSet,
    TeamViewSet,
)

router = DefaultRouter()
router.register("projects", ProjectViewSet, basename="project")
router.register("teams", TeamViewSet, basename="team")
router.register("templates", FormTemplateViewSet, basename="template")
router.register("records", FormRecordViewSet, basename="record")
router.register("conflicts", ConflictViewSet, basename="conflict")

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("", include(router.urls)),
]
