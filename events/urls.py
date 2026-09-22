from django.urls import path

from .views import EventBatchView

app_name = "events"

urlpatterns = [
    path("events/batch/", EventBatchView.as_view(), name="event-batch"),
]
