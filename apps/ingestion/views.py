from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.status import HTTP_202_ACCEPTED, HTTP_207_MULTI_STATUS, HTTP_400_BAD_REQUEST
from rest_framework.views import APIView

from apps.catalog.models import App
from .services import BatchTooLarge, ingest_batch


class EventBatchView(APIView):
    """POST /api/sdk/events/  with header X-SDK-Key.

    Body::

        {"events": [ {event_id, event_type, event_time, placement_code,
                      network_code, revenue?, error_code?, config_version_id?,
                      experiment_id?, experiment_variant?, user_key_hash?}, ... ]}

    Status codes:
      202 - whole batch accepted (or only idempotent duplicates)
      207 - mixed: some items accepted, some rejected (per-item detail)
      400 - batch-level error (empty/oversized/malformed) or nothing accepted
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        sdk_key = request.headers.get("X-SDK-Key", "")
        app = App.objects.filter(sdk_key=sdk_key.strip()).first() if sdk_key else None
        if app is None:
            return Response(
                {"message": "Invalid SDK key", "errors": None}, status=401
            )

        body = request.data
        if not isinstance(body, dict):
            return Response(
                {"message": "Request body must be a JSON object", "errors": None},
                status=HTTP_400_BAD_REQUEST,
            )
        raw_events = body.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            return Response(
                {
                    "message": "'events' must be a non-empty JSON array",
                    "errors": {"events": "required, non-empty array"},
                },
                status=HTTP_400_BAD_REQUEST,
            )

        try:
            result = ingest_batch(app=app, raw_events=raw_events)
        except BatchTooLarge as exc:
            return Response(
                {
                    "message": str(exc),
                    "errors": {"events": "batch_too_large"},
                },
                status=HTTP_400_BAD_REQUEST,
            )

        payload = result.as_dict()
        if result.failed == 0:
            return Response(payload, status=HTTP_202_ACCEPTED)
        if result.accepted == 0 and result.duplicates == 0:
            payload["message"] = "All events in the batch were rejected"
            return Response(payload, status=HTTP_400_BAD_REQUEST)
        return Response(payload, status=HTTP_207_MULTI_STATUS)
