from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("projects", views.ProjectViewSet, basename="project")
router.register("crews", views.CrewViewSet, basename="crew")
router.register("templates", views.FormTemplateViewSet, basename="template")

urlpatterns = [
    path(
        "templates/<int:pk>/versions/",
        views.TemplateVersionListView.as_view(),
        name="template-version-list",
    ),
    path(
        "templates/<int:pk>/versions/publish/",
        views.TemplateVersionPublishView.as_view(),
        name="template-version-publish",
    ),
    path(
        "templates/<int:pk>/versions/<int:version>/",
        views.TemplateVersionDetailView.as_view(),
        name="template-version-detail",
    ),
]

urlpatterns += router.urls
