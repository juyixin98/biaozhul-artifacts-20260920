from rest_framework.routers import DefaultRouter

from .views import DeveloperViewSet

router = DefaultRouter()
router.register(r"", DeveloperViewSet, basename="auth")

urlpatterns = router.urls
