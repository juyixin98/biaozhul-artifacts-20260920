from django.urls import path

from . import views

urlpatterns = [
    path("submit/", views.BatchSubmitView.as_view(), name="sync-submit"),
    path(
        "batches/<str:client_batch_id>/",
        views.BatchResultView.as_view(),
        name="sync-batch-result",
    ),
    path("pull/", views.PullView.as_view(), name="sync-pull"),
    path("conflicts/", views.PendingConflictsView.as_view(), name="sync-conflicts"),
    path(
        "conflicts/<uuid:record_uuid>/resolve/",
        views.ConflictResolveView.as_view(),
        name="sync-conflict-resolve",
    ),
    path(
        "records/<uuid:record_uuid>/",
        views.RecordDetailView.as_view(),
        name="sync-record-detail",
    ),
    path(
        "records/<uuid:record_uuid>/delete/",
        views.RecordDeleteView.as_view(),
        name="sync-record-delete",
    ),
]
