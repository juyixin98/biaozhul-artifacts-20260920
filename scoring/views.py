"""Read API for scoring results (tenant scoped to the developer's placements)."""
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from applications.models import Placement
from common.permissions import IsDeveloper

from .models import ScoreItem, ScoreRun
from .slots import floor_to_slot


class ScoreItemSerializer(serializers.ModelSerializer):
    network_id = serializers.IntegerField()
    network_code = serializers.CharField(source="network.code")
    placement_id = serializers.IntegerField()

    class Meta:
        model = ScoreItem
        fields = [
            "placement_id",
            "network_id",
            "network_code",
            "rank",
            "total_score",
            "fill_rate",
            "ecpm",
            "reliability",
            "norm_fill_rate",
            "norm_ecpm",
            "norm_reliability",
            "fills",
            "failures",
            "errors",
            "impressions",
            "revenue",
            "opportunities",
        ]


class ScoreRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScoreRun
        fields = [
            "id",
            "slot_start",
            "window_start",
            "window_end",
            "finalized",
            "items_hash",
            "finalized_at",
        ]


class LatestScoresView(APIView):
    permission_classes = [IsDeveloper]

    def get(self, request):
        run = ScoreRun.objects.filter(finalized=True).order_by("-slot_start").first()
        if run is None:
            return Response(
                {"detail": "no scoring run has been finalized yet"}, status=404
            )
        return self._serialize(run, request)

    def _serialize(self, run, request):
        placement_id = request.query_params.get("placement_id")
        items = run.items.select_related("network", "placement")
        owned_placement_ids = set(
            Placement.objects.filter(
                app__developer=request.user
            ).values_list("id", flat=True)
        )
        if placement_id is not None:
            try:
                placement_id = int(placement_id)
            except (TypeError, ValueError):
                return Response({"detail": "placement_id must be an integer"}, status=400)
            if placement_id not in owned_placement_ids:
                return Response({"detail": "placement not found in your account"}, status=404)
            items = items.filter(placement_id=placement_id)
        else:
            items = items.filter(placement_id__in=owned_placement_ids)
        return Response(
            {
                "run": ScoreRunSerializer(run).data,
                "methodology": {
                    "weights": {"fill_rate": 0.40, "ecpm": 0.35, "reliability": 0.25},
                    "window_days": 7,
                    "slot_minutes": 30,
                    "normalization": "per-placement min-max; networks without a "
                                     "metric sample are excluded from its reference "
                                     "population; max==min -> 1.0",
                    "missing_samples": "raw metrics 0 and normalized 0; no-sample "
                                       "networks score 0 overall",
                    "tie_break": "total desc, fill_rate desc, ecpm desc, "
                                 "reliability desc, network_id asc",
                },
                "items": ScoreItemSerializer(items, many=True).data,
            }
        )


class ScoreRunDetailView(APIView):
    permission_classes = [IsDeveloper]

    def get(self, request, slot_start):
        try:
            slot = floor_to_slot(_parse_dt(slot_start))
        except ValueError:
            return Response({"detail": "invalid slot timestamp"}, status=400)
        run = ScoreRun.objects.filter(slot_start=slot, finalized=True).first()
        if run is None:
            return Response({"detail": "no finalized run for this slot"}, status=404)
        return LatestScoresView()._serialize(run, request)


def _parse_dt(value: str):
    from datetime import datetime

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)
