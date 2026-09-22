from rest_framework import serializers

from .models import (
    AnalysisResult,
    AnalysisTask,
    Course,
    StyleSample,
    Submission,
    SubmissionBatch,
)


class CourseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ["id", "name", "created_at"]
        read_only_fields = ["created_at"]

    def create(self, validated_data):
        validated_data["teacher"] = self.context["request"].user
        return super().create(validated_data)


class StyleSampleSerializer(serializers.ModelSerializer):
    class Meta:
        model = StyleSample
        fields = ["id", "course", "label", "name", "content_hash",
                  "feature_version", "created_at"]
        read_only_fields = ["content_hash", "feature_version", "created_at"]


class SubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Submission
        fields = [
            "id", "course", "batch", "student_label", "filename", "source_format",
            "content_hash", "char_count", "duplicate_of", "created_at",
        ]
        read_only_fields = fields


class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisTask
        fields = [
            "id", "submission", "status", "progress", "stage", "attempts",
            "max_attempts", "worker_id", "error_code", "error_message",
            "error_detail", "created_at", "started_at", "finished_at",
        ]
        read_only_fields = fields


class ResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisResult
        fields = [
            "id", "submission", "task", "version", "input_hash",
            "algorithm_version", "metrics", "methodology", "created_at",
        ]
        read_only_fields = fields


class BatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubmissionBatch
        fields = ["id", "course", "note", "created_at"]
        read_only_fields = ["created_at"]
