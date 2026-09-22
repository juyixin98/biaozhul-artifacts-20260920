"""SDK-facing event batch endpoint."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import AppApiKeyAuthentication

from .services import BatchRejected, ingest_batch


class EventBatchView(APIView):
    authentication_classes = [AppApiKeyAuthentication]
    permission_classes = [IsAuthenticated]  # requires a resolved app principal

    def post(self, request):
        app = request.user.app
        try:
            summary = ingest_batch(app=app, data=request.data)
        except BatchRejected as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(summary, status=200)
