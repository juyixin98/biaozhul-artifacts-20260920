from django.urls import path

from .views import EventBatchView

urlpatterns = [
    path("sdk/events/", EventBatchView.as_view(), name="sdk-events"),
]
