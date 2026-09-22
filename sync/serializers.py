import uuid

from rest_framework import serializers
from django.conf import settings


class SyncEntrySerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    template_id = serializers.IntegerField()
    template_version = serializers.IntegerField(min_value=1)
    record_version = serializers.IntegerField(min_value=0, default=0)
    crew_id = serializers.IntegerField()
    collected_at = serializers.DateTimeField()
    data = serializers.DictField()


class SyncBatchRequestSerializer(serializers.Serializer):
    client_batch_id = serializers.CharField(max_length=64)
    entries = serializers.ListField(
        child=SyncEntrySerializer(),
        allow_empty=False,
        max_length=settings.MAX_RECORDS_PER_BATCH,
    )


class ResolveConflictSerializer(serializers.Serializer):
    merged_data = serializers.DictField()
    # Validate against the template version the supervisor chooses for the
    # merged record (usually the newest), not against the device's old one.
    template_version = serializers.IntegerField(min_value=1)
    note = serializers.CharField(min_length=1, max_length=2000)
