from django.urls import path

from . import views

app_name = "engine"

urlpatterns = [
    path("auth/token/", views.get_api_token),
    path("courses/", views.CourseListCreate.as_view(), name="course-list"),
    path("courses/<int:pk>/", views.CourseDetail.as_view(), name="course-detail"),
    path("courses/<int:course_id>/samples/", views.samples, name="samples"),
    path("courses/<int:course_id>/submissions/", views.SubmissionList.as_view(),
         name="submission-list"),
    path("courses/<int:course_id>/submit-batch/", views.submit_batch,
         name="submit-batch"),
    path("courses/<int:course_id>/batches/<int:batch_id>/", views.batch_status,
         name="batch-status"),
    path("courses/<int:course_id>/tasks/<int:task_id>/", views.task_status,
         name="task-status"),
    path("courses/<int:course_id>/submissions/<int:submission_id>/",
         views.submission_detail, name="submission-detail"),
    path(
        "courses/<int:course_id>/submissions/<int:submission_id>/results/<int:version>/",
        views.result_detail, name="result-detail",
    ),
]
