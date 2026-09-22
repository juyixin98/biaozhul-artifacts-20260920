from django.urls import path

from .views import (
    ExperimentStartView,
    ExperimentStatsView,
    ExperimentStopView,
    ExperimentViewSet,
)

app_name = "experiments"

_experiment_list = ExperimentViewSet.as_view({"get": "list", "post": "create"})
_experiment_detail = ExperimentViewSet.as_view({"get": "retrieve", "delete": "destroy"})

urlpatterns = [
    path("experiments/", _experiment_list, name="experiment-list"),
    path("experiments/<int:pk>/", _experiment_detail, name="experiment-detail"),
    path("experiments/<int:pk>/start/", ExperimentStartView.as_view(), name="experiment-start"),
    path("experiments/<int:pk>/stop/", ExperimentStopView.as_view(), name="experiment-stop"),
    path("experiments/<int:pk>/stats/", ExperimentStatsView.as_view(), name="experiment-stats"),
]
