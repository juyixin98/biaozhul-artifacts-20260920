from django.contrib import admin

from .models import (
    AnalysisResult,
    AnalysisTask,
    Course,
    Document,
    SubmissionBatch,
    UploadAttempt,
)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "owner", "created_at")
    filter_horizontal = ("teachers",)


@admin.register(SubmissionBatch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("id", "course", "uploaded_by", "created_at", "note")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "course", "filename", "content_sha256",
                    "word_count", "created_at")
    list_filter = ("course",)
    search_fields = ("filename", "content_sha256")


@admin.register(UploadAttempt)
class UploadAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "batch", "filename", "status", "document", "detail")
    list_filter = ("status", "course")


@admin.register(AnalysisTask)
class AnalysisTaskAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "status", "stage", "attempts",
                    "generation", "worker_id", "lease_expires_at")
    list_filter = ("status", "stage")


@admin.register(AnalysisResult)
class AnalysisResultAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "version", "algorithm_version",
                    "input_sha256", "created_at")
