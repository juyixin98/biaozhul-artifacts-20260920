from rest_framework import serializers

from .models import NetworkScore, ScoreRun


class NetworkScoreSerializer(serializers.ModelSerializer):
    network_code = serializers.CharField(source="network.code", read_only=True)

    class Meta:
        model = NetworkScore
        fields = (
            "id",
            "placement",
            "network",
            "network_code",
            "impressions",
            "fills",
            "failures",
            "revenue",
            "fill_rate",
            "ecpm",
            "reliability",
            "fill_rate_norm",
            "ecpm_norm",
            "reliability_norm",
            "score",
            "rank",
        )


class ScoreRunSerializer(serializers.ModelSerializer):
    network_scores = NetworkScoreSerializer(many=True, read_only=True)

    class Meta:
        model = ScoreRun
        fields = (
            "id",
            "period_start",
            "period_end",
            "window_start",
            "window_end",
            "status",
            "is_active",
            "checksum",
            "created_at",
            "completed_at",
            "network_scores",
        )
