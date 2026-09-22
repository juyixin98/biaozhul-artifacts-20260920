from rest_framework import serializers

from .models import Experiment, VariantVersion


class VariantVersionSerializer(serializers.ModelSerializer):
    version_number = serializers.IntegerField(source="version.version_number", read_only=True)

    class Meta:
        model = VariantVersion
        fields = ["variant", "version", "version_number"]
        read_only_fields = ["version_number"]


class ExperimentSerializer(serializers.ModelSerializer):
    variants = VariantVersionSerializer(many=True, read_only=True)
    version_a = serializers.IntegerField(write_only=True)
    version_b = serializers.IntegerField(write_only=True)
    placement_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = Experiment
        fields = [
            "id",
            "name",
            "public_key",
            "status",
            "created_at",
            "started_at",
            "stopped_at",
            "variants",
            "version_a",
            "version_b",
            "placement_id",
        ]
        read_only_fields = ["public_key", "status", "created_at", "started_at", "stopped_at"]
