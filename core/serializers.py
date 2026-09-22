"""DRF 序列化器。"""
from rest_framework import serializers

from .models import (
    Conflict,
    FormRecord,
    FormTemplate,
    Project,
    RecordVersion,
    Team,
)


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("id", "code", "name", "is_active", "created_at")


class TeamSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = (
            "id",
            "code",
            "name",
            "projects",
            "supervisors",
            "members",
            "created_at",
        )
        read_only_fields = ("created_at",)


class FormTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = FormTemplate
        fields = (
            "id",
            "project",
            "code",
            "version",
            "name",
            "status",
            "schema",
            "legacy_fields",
            "version_notes",
            "created_by",
            "created_at",
            "published_at",
            "deprecated_at",
        )
        read_only_fields = (
            "id",
            "version",
            "status",
            "legacy_fields",
            "created_by",
            "created_at",
            "published_at",
            "deprecated_at",
        )


class RecordVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecordVersion
        fields = (
            "id",
            "template",
            "content",
            "client_record_version",
            "collected_at",
            "device_id",
            "status",
            "created_by",
            "created_at",
        )
        read_only_fields = fields


class FormRecordSerializer(serializers.ModelSerializer):
    current_version = RecordVersionSerializer(read_only=True)
    uuid = serializers.UUIDField(read_only=True)

    class Meta:
        model = FormRecord
        fields = (
            "uuid",
            "project",
            "template",
            "status",
            "current_version",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = fields


class ConflictSerializer(serializers.ModelSerializer):
    versions = RecordVersionSerializer(many=True, read_only=True)
    winning_version = RecordVersionSerializer(read_only=True)
    record_uuid = serializers.UUIDField(source="record.uuid", read_only=True)

    class Meta:
        model = Conflict
        fields = (
            "id",
            "record",
            "record_uuid",
            "project",
            "status",
            "versions",
            "winning_version",
            "resolution_note",
            "resolved_by",
            "created_at",
            "resolved_at",
        )
        read_only_fields = fields


class SyncPushItemSerializer(serializers.Serializer):
    """故意保持宽松：单条格式错误作为条目级结果返回（HTTP 207），
    而不是让整批 400。真正的校验由 services.sync.process_item 完成。"""

    uuid = serializers.CharField(max_length=64)
    template_code = serializers.CharField(
        max_length=64, required=False, allow_blank=True
    )
    template_version = serializers.IntegerField(
        required=False, allow_null=True
    )
    record_version = serializers.IntegerField(
        required=False, allow_null=True
    )
    collected_at = serializers.CharField(required=False, allow_blank=True)
    device_id = serializers.CharField(max_length=128, required=False, allow_blank=True)
    data = serializers.DictField(required=False)


class SyncPushSerializer(serializers.Serializer):
    client_batch_id = serializers.CharField(max_length=128)
    device_id = serializers.CharField(max_length=128, required=False, allow_blank=True)
    items = SyncPushItemSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("items 不能为空")
        return value


class ResolveConflictSerializer(serializers.Serializer):
    winning_version_id = serializers.IntegerField(min_value=1)
    note = serializers.CharField(
        min_length=2,
        max_length=2000,
        help_text="解决依据：必填，说明为何保留该版本，供审计。",
    )
