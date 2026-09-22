"""Management endpoints for waterfall drafts / versions.

    GET    /api/v1/placements/{placement_pk}/versions/
    GET    /api/v1/placements/{placement_pk}/config/
    PUT    /api/v1/placements/{placement_pk}/draft/
    POST   /api/v1/placements/{placement_pk}/publish/
"""
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from applications.models import Placement
from common.exceptions import ConflictError, DomainError, NotFoundError
from common.permissions import IsDeveloper

from .models import CurrentVersion, WaterfallVersion
from .serializers import (
    DraftWriteSerializer,
    WaterfallVersionSerializer,
)
from .services import create_or_replace_draft, publish_draft


def _domain_response(exc: DomainError) -> Response:
    return Response({"detail": str(exc)}, status=exc.status_code)


class _PlacementScopedView(APIView):
    permission_classes = [IsDeveloper]

    def get_placement(self, pk):
        placement = get_object_or_404(
            Placement.objects.select_related("app"), pk=pk
        )
        if placement.app.developer_id != self.request.user.id:
            # Do not leak existence of another tenant's placements.
            raise PermissionDenied("placement not found in your account")
        return placement


class VersionListView(_PlacementScopedView):
    def get(self, request, placement_pk):
        placement = self.get_placement(placement_pk)
        versions = (
            WaterfallVersion.objects.filter(placement=placement)
            .prefetch_related("entries__network")
            .order_by("-version_number", "-id")
        )
        return Response(WaterfallVersionSerializer(versions, many=True).data)


class DraftView(_PlacementScopedView):
    def put(self, request, placement_pk):
        placement = self.get_placement(placement_pk)
        serializer = DraftWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            draft = create_or_replace_draft(
                placement=placement,
                entries_data=serializer.validated_data["entries"],
                developer=request.user,
                note=serializer.validated_data.get("note", ""),
            )
        except DomainError as exc:
            return _domain_response(exc)
        draft = (
            WaterfallVersion.objects.prefetch_related("entries__network")
            .get(pk=draft.pk)
        )
        return Response(
            WaterfallVersionSerializer(draft).data,
            status=status.HTTP_200_OK,
        )


class PublishView(_PlacementScopedView):
    def post(self, request, placement_pk):
        placement = self.get_placement(placement_pk)
        try:
            version = publish_draft(placement=placement, developer=request.user)
        except (NotFoundError, ConflictError) as exc:
            return _domain_response(exc)
        version = (
            WaterfallVersion.objects.prefetch_related("entries__network")
            .get(pk=version.pk)
        )
        return Response(WaterfallVersionSerializer(version).data, status=200)


class CurrentConfigView(_PlacementScopedView):
    def get(self, request, placement_pk):
        placement = self.get_placement(placement_pk)
        pointer = (
            CurrentVersion.objects.filter(placement=placement)
            .select_related("version")
            .prefetch_related("version__entries__network")
            .first()
        )
        if pointer is None:
            return Response(
                {"detail": "no configuration has been published yet"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(WaterfallVersionSerializer(pointer.version).data)
