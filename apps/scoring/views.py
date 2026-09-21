from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from apps.catalog.models import Placement
from apps.common.permissions import IsOwner
from .models import NetworkScore, ScoreRun
from .serializers import NetworkScoreSerializer, ScoreRunSerializer
from .services import run_latest


class ScoreRunViewSet(viewsets.ReadOnlyModelViewSet):
    """Scoring runs (developer-facing, scoped to owned placements).

    GET  /api/score-runs/                 list runs
    GET  /api/score-runs/{id}/            run + scores
    GET  /api/score-runs/active/?placement=<id>
                                          currently active scores for a
                                          placement
    POST /api/score-runs/run_now/         trigger scoring for the latest
                                          closed period (repeat-safe)
    """

    serializer_class = ScoreRunSerializer
    permission_classes = [IsOwner]

    def get_queryset(self):
        qs = ScoreRun.objects.prefetch_related(
            "network_scores__network"
        ).all()
        placement_id = self.request.query_params.get("placement")
        if placement_id:
            qs = qs.filter(network_scores__placement_id=placement_id).distinct()
        return qs

    @action(detail=False, methods=["get"], url_path="active")
    def active(self, request):
        placement_id = request.query_params.get("placement")
        placement = get_object_or_404(Placement, id=placement_id)
        if placement.app.owner_id != request.user.id:
            self.permission_denied(request, "Not your placement.")
        run = ScoreRun.objects.filter(is_active=True).first()
        if run is None:
            return Response(
                {"message": "No active scoring run yet", "errors": None}, status=404
            )
        scores = NetworkScore.objects.filter(
            run=run, placement=placement
        ).select_related("network")
        return Response(
            {
                "run": ScoreRunSerializer(run).data,
                "scores": NetworkScoreSerializer(scores, many=True).data,
            }
        )

    @action(detail=False, methods=["post"], url_path="run_now")
    def run_now(self, request):
        force = str(request.data.get("force", "")).lower() in {"1", "true", "yes"}
        run, created = run_latest(force=force)
        return Response(
            {
                "run": ScoreRunSerializer(run).data,
                "created_or_refreshed": created,
            },
            status=201 if created else 200,
        )
