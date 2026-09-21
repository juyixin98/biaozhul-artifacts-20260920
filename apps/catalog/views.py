from django.db import IntegrityError, transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import IsOwner
from .models import (
    AdNetwork,
    App,
    AuditLog,
    ConfigVersion,
    Placement,
    PlacementNetwork,
)
from .serializers import (
    AdNetworkSerializer,
    AppSerializer,
    AuditLogSerializer,
    ConfigVersionSerializer,
    PlacementNetworkSerializer,
    PlacementSerializer,
)
from .services import PublishError, publish_config, record_audit


class AppViewSet(viewsets.ModelViewSet):
    """Developer apps. Users only ever see and modify their own apps."""

    serializer_class = AppSerializer
    permission_classes = [IsOwner]
    queryset = App.objects.all()

    def get_queryset(self):
        return App.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        app = serializer.save(owner=self.request.user)
        record_audit(
            actor=self.request.user,
            app=app,
            action=AuditLog.Action.APP_CREATE,
            target_type="App",
            target=app,
            payload={"name": app.name, "code": app.code},
        )


class PlacementViewSet(viewsets.ModelViewSet):
    serializer_class = PlacementSerializer
    permission_classes = [IsOwner]

    def get_queryset(self):
        qs = Placement.objects.filter(app__owner=self.request.user).select_related(
            "app", "active_version"
        )
        app_id = self.kwargs.get("app_pk")
        if app_id is not None:
            qs = qs.filter(app_id=app_id)
        app_code = self.request.query_params.get("app_code")
        if app_code:
            qs = qs.filter(app__code=app_code)
        return qs

    def perform_create(self, serializer):
        placement = serializer.save()
        record_audit(
            actor=self.request.user,
            app=placement.app,
            action=AuditLog.Action.PLACEMENT_CREATE,
            target_type="Placement",
            target=placement,
            payload={"code": placement.code, "format": placement.format},
        )

    def perform_update(self, serializer):
        placement = serializer.save()
        record_audit(
            actor=self.request.user,
            app=placement.app,
            action=AuditLog.Action.PLACEMENT_UPDATE,
            target_type="Placement",
            target=placement,
            payload=serializer.validated_data,
        )


class PlacementNetworkViewSet(viewsets.ModelViewSet):
    """Editable waterfall draft lines."""

    serializer_class = PlacementNetworkSerializer
    permission_classes = [IsOwner]

    def get_queryset(self):
        qs = PlacementNetwork.objects.filter(
            placement__app__owner=self.request.user
        ).select_related("placement", "network")
        placement_id = self.request.query_params.get("placement")
        if placement_id:
            qs = qs.filter(placement_id=placement_id)
        return qs

    def _audit(self, action, line, payload):
        record_audit(
            actor=self.request.user,
            app=line.placement.app,
            action=action,
            target_type="PlacementNetwork",
            target=line,
            target_repr=f"{line.placement.code}:{line.network.code}",
            payload=payload,
        )

    def perform_create(self, serializer):
        try:
            with transaction.atomic():
                line = serializer.save()
        except IntegrityError as exc:
            raise ValidationError(
                "Conflicting network line (duplicate network or priority)."
            ) from exc
        self._audit(
            AuditLog.Action.NETWORK_ADD,
            line,
            {
                "network": line.network.code,
                "priority": line.priority,
                "cpm_floor": str(line.cpm_floor),
                "fallback_order": line.fallback_order,
                "enabled": line.enabled,
            },
        )

    def perform_update(self, serializer):
        before = {
            "network": serializer.instance.network.code,
            "priority": serializer.instance.priority,
            "cpm_floor": str(serializer.instance.cpm_floor),
            "fallback_order": serializer.instance.fallback_order,
            "enabled": serializer.instance.enabled,
        }
        try:
            with transaction.atomic():
                line = serializer.save()
        except IntegrityError as exc:
            raise ValidationError(
                "Conflicting network line (duplicate network or priority)."
            ) from exc
        self._audit(
            AuditLog.Action.NETWORK_UPDATE,
            line,
            {"before": before, "after": serializer.validated_data},
        )

    def perform_destroy(self, instance):
        payload = {
            "placement": instance.placement.code,
            "network": instance.network.code,
        }
        app = instance.placement.app
        with transaction.atomic():
            instance.delete()
        record_audit(
            actor=self.request.user,
            app=app,
            action=AuditLog.Action.NETWORK_REMOVE,
            target_type="PlacementNetwork",
            target_repr=f"{instance.placement.code}:{instance.network.code}",
            payload=payload,
        )


class ConfigVersionViewSet(viewsets.ReadOnlyModelViewSet):
    """Published configuration versions (immutable) + publish action."""

    serializer_class = ConfigVersionSerializer
    permission_classes = [IsOwner]

    def get_queryset(self):
        qs = ConfigVersion.objects.filter(
            placement__app__owner=self.request.user
        ).select_related("placement", "published_by")
        placement_id = self.request.query_params.get("placement")
        if placement_id:
            qs = qs.filter(placement_id=placement_id)
        return qs.prefetch_related("entries")

    @action(detail=False, methods=["post"], url_path="publish")
    def publish(self, request):
        """POST /api/config-versions/publish/ {placement: <id>, note: ...}"""
        placement_id = request.data.get("placement")
        placement = get_object_or_404(
            Placement.objects.select_related("app"),
            pk=placement_id,
        )
        if placement.app.owner_id != request.user.id:
            # Do not reveal existence of someone else's placement.
            raise PermissionDenied("Placement not found or not owned by you.")
        note = str(request.data.get("note", ""))[:255]
        try:
            version = publish_config(
                placement=placement, published_by=request.user, note=note
            )
        except PublishError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(
            ConfigVersionSerializer(version).data, status=status.HTTP_201_CREATED
        )


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditLogSerializer
    permission_classes = [IsOwner]

    def get_queryset(self):
        qs = AuditLog.objects.filter(app__owner=self.request.user).select_related(
            "actor", "app"
        )
        app_id = self.request.query_params.get("app")
        if app_id:
            qs = qs.filter(app_id=app_id)
        action = self.request.query_params.get("action")
        if action:
            qs = qs.filter(action=action)
        return qs


class AdNetworkViewSet(viewsets.ReadOnlyModelViewSet):
    """Global, read-only catalog of supported ad networks."""

    queryset = AdNetwork.objects.filter(is_active=True)
    serializer_class = AdNetworkSerializer


# ---------------------------------------------------------------------------
# SDK endpoints
# ---------------------------------------------------------------------------


class SDKCurrentConfigView(APIView):
    """Return the currently active immutable config for a placement.

    Authentication is *not* the developer token: SDKs present the per-app
    ``X-SDK-Key`` header. GET only; the response never exposes draft data.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request, app_code, placement_code):
        sdk_key = request.headers.get("X-SDK-Key", "")
        app = (
            App.objects.filter(code=app_code)
            .prefetch_related("placements__active_version__entries__network")
            .first()
        )
        if app is None or not _sdk_key_matches(app, sdk_key):
            return Response(
                {"message": "Invalid SDK key", "errors": None},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        placement = next(
            (p for p in app.placements.all() if p.code == placement_code),
            None,
        )
        if placement is None:
            return Response(
                {"message": "Unknown placement", "errors": None},
                status=status.HTTP_404_NOT_FOUND,
            )
        if placement.active_version is None:
            return Response(
                {"message": "No published configuration yet", "errors": None},
                status=status.HTTP_404_NOT_FOUND,
            )
        version = placement.active_version
        return Response(
            {
                "app_code": app.code,
                "placement_code": placement.code,
                "config_version": version.version,
                "config_version_id": version.id,
                "published_at": version.published_at,
                "waterfall": [
                    {
                        "network_code": e.network_code,
                        "network_name": e.network_name,
                        "priority": e.priority,
                        "cpm_floor": str(e.cpm_floor),
                        "fallback_order": e.fallback_order,
                    }
                    for e in sorted(version.entries.all(), key=lambda e: e.priority)
                ],
            }
        )


def _sdk_key_matches(app, sdk_key) -> bool:
    if not sdk_key:
        return False
    return str(app.sdk_key) == str(sdk_key).strip()
