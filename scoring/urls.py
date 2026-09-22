from django.urls import path, re_path

from .views import LatestScoresView, ScoreRunDetailView

app_name = "scoring"

urlpatterns = [
    path("scores/", LatestScoresView.as_view(), name="scores-latest"),
    re_path(
        r"^scores/(?P<slot_start>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?)/$",
        ScoreRunDetailView.as_view(),
        name="scores-run",
    ),
]
