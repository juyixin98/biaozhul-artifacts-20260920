from django.urls import path

from . import views

urlpatterns = [
    # Pools
    path("pools/", views.PoolListCreateView.as_view(), name="pool-list"),
    path("pools/<int:pk>/", views.PoolDetailView.as_view(), name="pool-detail"),

    # Nodes
    path("nodes/", views.NodeListCreateView.as_view(), name="node-list"),
    path("nodes/register/", views.NodeRegisterView.as_view(), name="node-register"),
    path("nodes/heartbeat/", views.NodeHeartbeatView.as_view(), name="node-heartbeat"),
    path(
        "nodes/<int:pk>/state/",
        views.NodeAdminStateView.as_view(),
        name="node-admin-state",
    ),

    # Jobs
    path("jobs/", views.JobListCreateView.as_view(), name="job-list"),
    path("jobs/<int:pk>/", views.JobDetailView.as_view(), name="job-detail"),
    path("jobs/<int:pk>/cancel/", views.JobCancelView.as_view(), name="job-cancel"),
    path(
        "jobs/<int:pk>/complete/",
        views.JobCompleteView.as_view(),
        name="job-complete",
    ),
    path(
        "jobs/<int:pk>/release/",
        views.JobReleasePreemptedView.as_view(),
        name="job-release-preempted",
    ),

    # Observability
    path(
        "allocations/",
        views.AllocationListView.as_view(),
        name="allocation-list",
    ),
    path(
        "preemptions/",
        views.PreemptionListView.as_view(),
        name="preemption-list",
    ),
    path("decisions/", views.DecisionListView.as_view(), name="decision-list"),

    # Manual scheduler trigger
    path("scheduler/tick/", views.SchedulerTickView.as_view(), name="scheduler-tick"),
]
