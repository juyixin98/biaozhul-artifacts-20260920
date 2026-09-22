from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BatchUploadView,
    BatchViewSet,
    CourseViewSet,
    DocumentViewSet,
    ResultViewSet,
    TaskViewSet,
)

router = DefaultRouter()
router.register("courses", CourseViewSet, basename="course")
router.register("batches", BatchViewSet, basename="batch")
router.register("documents", DocumentViewSet, basename="document")
router.register("tasks", TaskViewSet, basename="task")
router.register("results", ResultViewSet, basename="result")

urlpatterns = [
    path("batches/upload/", BatchUploadView.as_view(), name="batch-upload"),
    path("", include(router.urls)),
]
