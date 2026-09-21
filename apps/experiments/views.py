from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import App, Placement
from apps.common.permissions import IsOwner
from .models import Experiment
from .serializers import ExperimentSerializer
from .services import (
    ExperimentError,
    assign_variant,
    experiment_stats,
    stop_experiment,
    variant_config_version,
)


class ExperimentViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ExperimentSerializer
    permission_classes = [IsOwner]

    def get_queryset(self):
        qs = Experiment.objects.filter(
            placement__app__owner=self.request.user
        ).select_related(
            "placement", "variant_a_version", "variant_b_version", "created_by"
        )
        placement_id = self.request.query_params.get("placement")
        if placement_id:
            qs = qs.filter(placement_id=placement_id)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        from rest_framework.exceptions import ValidationError

        from .services import create_experiment

        try:
            experiment = create_experiment(
                placement=data["placement"],
                created_by=request.user,
                name=data["name"],
                variant_a_version_id=data["variant_a_version"].id,
                variant_b_version_id=data["variant_b_version"].id,
            )
        except ExperimentError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(
            self.get_serializer(experiment).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="stop")
    def stop(self, request, pk=None):
        experiment = self.get_object()
        updated = stop_experiment(experiment=experiment, actor=request.user)
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["get"], url_path="stats")
    def stats(self, request, pk=None):
        experiment = self.get_object()
        data = experiment_stats(experiment)
        return Response(
            {
                "experiment": experiment.id,
                "name": experiment.name,
                "status": experiment.status,
                "placement_id": experiment.placement_id,
                "variants": data,
            }
        )


class SDKExperimentAssignView(APIView):
    """Resolve (and freeze) a user's variant for a placement's running
    experiment. Authenticated with the app SDK key.

    POST /api/sdk/apps/<app_code>/placements/<placement_code>/assign/
    {"user_key_hash": "<sha256 hex>"}

    Returns 404 when no experiment is running. The response carries the
    frozen config_version_id so the SDK can fetch that exact version and the
    client reports it back with every event.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request, app_code, placement_code):
        sdk_key = request.headers.get("X-SDK-Key", "")
        app = App.objects.filter(code=app_code).first()
        if app is None or not sdk_key or str(app.sdk_key) != sdk_key.strip():
            return Response(
                {"message": "Invalid SDK key", "errors": None}, status=401
            )
        placement = (
            Placement.objects.filter(app=app, code=placement_code)
            .prefetch_related(
                "experiments__variant_a_version",
                "experiments__variant_b_version",
            )
            .first()
        )
        if placement is None:
            return Response(
                {"message": "Unknown placement", "errors": None}, status=404
            )
        experiment = next(
            (
                e
                for e in placement.experiments.all()
                if e.status == Experiment.Status.RUNNING
            ),
            None,
        )
        if experiment is None:
            return Response(
                {"message": "No running experiment for this placement"},
                status=status.HTTP_404_NOT_FOUND,
            )

        user_key_hash = str(request.data.get("user_key_hash") or "").strip().lower()
        if not user_key_hash or len(user_key_hash) > 64:
            return Response(
                {
                    "message": "user_key_hash is required (max 64 chars)",
                    "errors": {"user_key_hash": "required"},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        variant = assign_variant(
            experiment=experiment, user_key_hash=user_key_hash
        )
        version = variant_config_version(experiment, variant)
        return Response(
            {
                "experiment_id": experiment.id,
                "variant": variant,
                "config_version_id": version.id,
                "config_version": version.version,
            }
        )
