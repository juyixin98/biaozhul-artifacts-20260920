"""SDK configuration resolution endpoint.

    GET /api/v1/sdk/config/?placement_key=home_rewarded&user_key=device-uuid

Auth: ``Authorization: ApiKey <app.api_key>``.

Resolution rules
----------------
* No running experiment for the placement -> serve the placement's current
  published version ("control").
* A running experiment + ``user_key`` -> deterministic 50/50 assignment; the
  bound, immutable variant version is served and the assignment is recorded.
* A running experiment without ``user_key`` -> control is served and the
  response flags ``user_key_required`` so the SDK can retry with a stable
  device id (assigning without a stable key would break fixed grouping).
* No published configuration -> 409.

The response carries the exact ``version_id`` the SDK must echo back on every
event; that is how impressions keep the config version they were served.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from applications.models import Placement
from common.permissions import AppApiKeyAuthentication
from experiments.models import Experiment
from experiments.services import assign_variant
from waterfall.models import CurrentVersion, WaterfallVersion
from waterfall.serializers import WaterfallVersionSerializer


class SdkConfigView(APIView):
    authentication_classes = [AppApiKeyAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        app = request.user.app
        placement_key = request.query_params.get("placement_key")
        if not placement_key:
            return Response(
                {"detail": "placement_key query parameter is required"}, status=400
            )
        try:
            placement = Placement.objects.get(app=app, placement_key=placement_key)
        except Placement.DoesNotExist:
            return Response({"detail": "unknown placement_key for this app"}, status=404)

        pointer = (
            CurrentVersion.objects.filter(placement=placement)
            .select_related("version")
            .prefetch_related("version__entries__network")
            .first()
        )
        if pointer is None:
            return Response(
                {"detail": "no configuration has been published for this placement"},
                status=409,
            )
        control = pointer.version

        experiment = (
            Experiment.objects.filter(
                placement=placement, status=Experiment.Status.RUNNING
            )
            .prefetch_related("variants__version__entries__network")
            .order_by("-started_at", "-id")
            .first()
        )

        response_version = control
        assignment = {"type": "control"}

        if experiment is not None:
            user_key = request.query_params.get("user_key")
            if not user_key:
                assignment = {
                    "type": "experiment_pending",
                    "experiment_id": experiment.id,
                    "experiment_key": experiment.public_key,
                    "user_key_required": True,
                }
            else:
                variant, version_id, tracked = assign_variant(
                    experiment=experiment, user_key=user_key
                )
                variant_version = (
                    WaterfallVersion.objects.prefetch_related("entries__network")
                    .get(pk=version_id)
                )
                response_version = variant_version
                assignment = {
                    "type": "experiment",
                    "experiment_id": experiment.id,
                    "experiment_key": experiment.public_key,
                    "variant": variant,
                    "assignment_recorded": tracked,
                }

        payload = WaterfallVersionSerializer(response_version).data
        payload["served"] = {
            "placement_key": placement.placement_key,
            "assignment": assignment,
        }
        return Response(payload)
