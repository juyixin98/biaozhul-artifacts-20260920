from django.urls import path

from . import views

urlpatterns = [
    path("projects/", views.ProjectListView.as_view(), name="project-list"),
    path(
        "projects/<int:project_id>/templates/",
        views.TemplateListCreateView.as_view(),
        name="template-list-create",
    ),
    path(
        "templates/<int:template_id>/versions/",
        views.VersionListCreateView.as_view(),
        name="version-list-create",
    ),
    path(
        "versions/<int:version_id>/publish/",
        views.VersionPublishView.as_view(),
        name="version-publish",
    ),
    path(
        "projects/<int:project_id>/cases/",
        views.CaseListCreateView.as_view(),
        name="case-list-create",
    ),
    path(
        "projects/<int:project_id>/events/import/",
        views.EventImportView.as_view(),
        name="event-import",
    ),
    path(
        "projects/<int:project_id>/cases/<str:case_key>/trace/",
        views.CaseTraceView.as_view(),
        name="case-trace",
    ),
    path(
        "projects/<int:project_id>/cases/<str:case_key>/deviations/",
        views.CaseDeviationView.as_view(),
        name="case-deviations",
    ),
    path(
        "projects/<int:project_id>/cases/<str:case_key>/analysis-history/",
        views.CaseAnalysisHistoryView.as_view(),
        name="case-analysis-history",
    ),
    path(
        "projects/<int:project_id>/reports/deviations/",
        views.ProjectDeviationReportView.as_view(),
        name="project-deviation-report",
    ),
    path(
        "projects/<int:project_id>/analysis/rebuild/",
        views.RebuildView.as_view(),
        name="analysis-rebuild",
    ),
]
