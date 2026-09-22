from rest_framework import serializers

from .models import Crew, FormTemplate, FormTemplateVersion, Project, validate_field_schema


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("id", "name", "description", "created_at")
        read_only_fields = ("id", "created_at")


class CrewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Crew
        fields = ("id", "project", "name", "created_at")
        read_only_fields = ("id", "created_at")


class TemplateVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FormTemplateVersion
        fields = (
            "id",
            "version",
            "fields",
            "min_supported_version",
            "published_by",
            "published_at",
        )
        read_only_fields = fields


class FormTemplateSerializer(serializers.ModelSerializer):
    current_version_number = serializers.IntegerField(read_only=True)

    class Meta:
        model = FormTemplate
        fields = (
            "id",
            "project",
            "code",
            "name",
            "current_version_number",
            "created_at",
        )
        read_only_fields = ("id", "current_version_number", "created_at")


class PublishVersionSerializer(serializers.Serializer):
    fields = serializers.ListField(child=serializers.DictField(), allow_empty=False)
    min_supported_version = serializers.IntegerField(required=False, min_value=1)

    def validate_fields(self, fields):
        validate_field_schema(fields)
        return fields
