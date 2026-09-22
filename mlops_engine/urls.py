from django.contrib import admin
from django.urls import include, path

from scheduler.views import APIRootView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", APIRootView.as_view(), name="api-root"),
    path("api/", include("scheduler.urls")),
]
