"""Experiment management API (tenant-scoped)."""
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from applications.models import Placement
from common.exceptions import DomainError
from common.permissions import IsDeveloper

from .models import Experiment
from .serializers import ExperimentSerializer
from .services import (
    create_experiment,
    experiment_stats,
    start_experiment,
    stop_experiment,
)


class ExperimentViewSet(viewsets.ModelViewSet):
    serializer_class = ExperimentSerializer
    permission_classes = [IsDeveloper]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return (
            Experiment.objects.filter(placement__app__developer=self.request.user)
            .select_related("placement__app")
            .prefetch_related("variants__version")
        )

    def _domain_response(self, exc: DomainError) -> Response:
        return Response({"detail": str(exc)}, status=exc.status_code)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        placement = get_object_or_404(
            Placement.objects.select_related("app"),
            id=data["placement_id"],
        )
        if placement.app.developer_id != request.user.id:
            raise PermissionDenied("placement not found in your account")
        try:
            experiment = create_experiment(
                placement=placement,
                name=data["name"],
                version_a_id=data["version_a"],
                version_b_id=data["version_b"],
                developer=request.user,
            )
        except DomainError as exc:
            return self._domain_response(exc)
        experiment = self.get_queryset().get(pk=experiment.pk)
        return Response(
            ExperimentSerializer(experiment).data,
            status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        experiment = self.get_object()
        if experiment.status == Experiment.Status.RUNNING:
            return Response(
                {"detail": "stop the experiment before deleting it"},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


class _ExperimentActionView(APIView):
    permission_classes = [IsDeveloper]

    def get_experiment(self, request, pk):
        experiment = get_object_or_404(
            Experiment.objects.select_related("placement__app"), pk=pk
        )
        if experiment.placement.app.developer_id != request.user.id:
            raise PermissionDenied("experiment not found in your account")
        return experiment


class ExperimentStartView(_ExperimentActionView):
    def post(self, request, pk):
        experiment = self.get_experiment(request, pk)
        try:
            experiment = start_experiment(experiment=experiment, developer=request.user)
        except DomainError as exc:
            return Response({"detail": str(exc)}, status=exc.status_code)
        return Response(ExperimentSerializer(experiment).data)


class ExperimentStopView(_ExperimentActionView):
    def post(self, request, pk):
        experiment = self.get_experiment(request, pk)
        try:
            experiment = stop_experiment(experiment=experiment, developer=request.user)
        except DomainError as exc:
            return Response({"detail": str(exc)}, status=exc.status_code)
        return Response(ExperimentSerializer(experiment).data)


class ExperimentStatsView(_ExperimentActionView):
    def get(self, request, pk):
        experiment = self.get_experiment(request, pk)
        return Response(
            {
                "experiment": experiment.public_key,
                "status": experiment.status,
                "variants": experiment_stats(experiment),
            }
        )
