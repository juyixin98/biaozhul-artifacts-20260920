from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"pools", views.ResourcePoolViewSet, basename="pool")
router.register(r"nodes", views.NodeViewSet, basename="node")
router.register(r"jobs", views.JobViewSet, basename="job")
router.register(r"decisions", views.SchedulingDecisionViewSet, basename="decision")
router.register(r"preemptions", views.PreemptionRecordViewSet,
                basename="preemption")

urlpatterns = [
    path("", include(router.urls)),
]
