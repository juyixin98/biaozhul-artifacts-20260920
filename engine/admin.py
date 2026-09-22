from django.contrib import admin

from .models import (
    AnalysisResult,
    AnalysisTask,
    Course,
    StyleSample,
    Submission,
    SubmissionBatch,
)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "teacher", "created_at")
    list_filter = ("teacher",)


@admin.register(SubmissionBatch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("id", "course", "submitted_by", "created_at")


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("id", "course", "filename", "content_hash", "created_at")
    list_filter = ("course",)


@admin.register(AnalysisTask)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "status", "stage", "attempts",
                    "worker_id", "run_token", "lease_expires_at")
    list_filter = ("status", "stage")


@admin.register(AnalysisResult)
class ResultAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "version", "algorithm_version", "created_at")


@admin.register(StyleSample)
class StyleSampleAdmin(admin.ModelAdmin):
    list_display = ("id", "course", "label", "name", "feature_version")
    list_filter = ("course", "label")
