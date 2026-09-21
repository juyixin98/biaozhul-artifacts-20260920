from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ScoreRunViewSet

router = DefaultRouter()
router.register(r"score-runs", ScoreRunViewSet, basename="scorerun")

urlpatterns = [path("", include(router.urls))]
