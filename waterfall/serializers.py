from rest_framework import serializers

from .models import CurrentVersion, WaterfallEntry, WaterfallVersion


class WaterfallEntrySerializer(serializers.ModelSerializer):
    network_id = serializers.IntegerField()
    network_code = serializers.CharField(source="network.code", read_only=True)
    floor_cpm = serializers.DecimalField(
        max_digits=14, decimal_places=6, coerce_to_string=True
    )

    class Meta:
        model = WaterfallEntry
        fields = [
            "network_id",
            "network_code",
            "priority",
            "fallback_order",
            "floor_cpm",
            "is_enabled",
        ]


class DraftWriteSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    entries = WaterfallEntrySerializer(many=True)


class WaterfallVersionSerializer(serializers.ModelSerializer):
    entries = serializers.SerializerMethodField()

    class Meta:
        model = WaterfallVersion
        fields = [
            "id",
            "version_number",
            "status",
            "note",
            "created_at",
            "updated_at",
            "published_at",
            "entries",
        ]

    def get_entries(self, obj):
        entries = obj.entries.select_related("network")
        return WaterfallEntrySerializer(entries, many=True).data


class CurrentVersionSerializer(serializers.ModelSerializer):
    placement_id = serializers.IntegerField(source="placement.id", read_only=True)
    version = WaterfallVersionSerializer(read_only=True)

    class Meta:
        model = CurrentVersion
        fields = ["placement_id", "updated_at", "version"]
