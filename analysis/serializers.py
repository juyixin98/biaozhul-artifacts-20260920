from rest_framework import serializers

from .models import (
    AnalysisResult,
    AnalysisTask,
    Course,
    Document,
    SubmissionBatch,
    UploadAttempt,
)


class CourseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ["id", "name", "owner", "teachers", "created_at"]
        read_only_fields = ["id", "owner", "created_at"]


class UploadAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = UploadAttempt
        fields = ["id", "filename", "size_bytes", "content_sha256",
                  "status", "document", "detail", "created_at"]


class BatchSerializer(serializers.ModelSerializer):
    attempts = UploadAttemptSerializer(many=True, read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = SubmissionBatch
        fields = ["id", "course", "note", "uploaded_by", "created_at",
                  "attempts", "progress"]
        read_only_fields = ["id", "uploaded_by", "created_at"]

    def get_progress(self, batch) -> dict:
        docs = Document.objects.filter(batch=batch)
        tasks = AnalysisTask.objects.filter(document__in=docs)
        totals = {
            "documents": docs.count(),
            "pending": tasks.filter(status=AnalysisTask.Status.PENDING).count(),
            "running": tasks.filter(status=AnalysisTask.Status.RUNNING).count(),
            "succeeded": tasks.filter(status=AnalysisTask.Status.SUCCEEDED).count(),
            "failed": tasks.filter(status=AnalysisTask.Status.FAILED).count(),
        }
        total = totals["documents"] or 0
        totals["percent"] = (
            round(100 * totals["succeeded"] / total, 1) if total else 100.0
        )
        return totals


class BatchListSerializer(serializers.ModelSerializer):
    document_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SubmissionBatch
        fields = ["id", "course", "note", "uploaded_by", "created_at",
                  "document_count"]


class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisTask
        fields = [
            "id", "document", "status", "stage", "attempts", "max_attempts",
            "worker_id", "leased_at", "lease_expires_at", "generation",
            "error_stage", "error_message",
            "created_at", "updated_at", "started_at", "finished_at",
        ]


class ResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisResult
        fields = ["id", "document", "version", "algorithm_version",
                  "input_sha256", "metrics_json", "created_at"]


class DocumentSerializer(serializers.ModelSerializer):
    results = ResultSerializer(many=True, read_only=True)
    latest_result = serializers.SerializerMethodField()
    latest_task = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id", "course", "batch", "filename", "content_type",
            "size_bytes", "content_sha256", "char_count", "word_count",
            "uploaded_by", "created_at", "results", "latest_result",
            "latest_task",
        ]

    def get_latest_result(self, doc):
        result = doc.results.order_by("-version").first()
        if result is None:
            return None
        return {
            "version": result.version,
            "algorithm_version": result.algorithm_version,
            "input_sha256": result.input_sha256,
            "metrics": result.metrics_json,
            "created_at": result.created_at,
        }

    def get_latest_task(self, doc):
        task = doc.tasks.order_by("-id").first()
        if task is None:
            return None
        return {
            "id": task.id,
            "status": task.status,
            "stage": task.stage,
            "attempts": task.attempts,
            "error_stage": task.error_stage,
            "error_message": task.error_message,
        }
