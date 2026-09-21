from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ExperimentViewSet, SDKExperimentAssignView

router = DefaultRouter()
router.register(r"experiments", ExperimentViewSet, basename="experiment")

urlpatterns = [
    path("", include(router.urls)),
    path(
        "sdk/apps/<str:app_code>/placements/<str:placement_code>/assign/",
        SDKExperimentAssignView.as_view(),
        name="sdk-experiment-assign",
    ),
]
