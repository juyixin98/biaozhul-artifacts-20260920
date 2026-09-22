from django.shortcuts import get_object_or_404
from rest_framework import status, views
from rest_framework.response import Response

from accounts.access import (
    accessible_crew_ids,
    accessible_project_ids,
    can_access_project,
    can_submit_to_crew,
    is_admin,
    is_supervisor,
    supervised_crew_ids,
)
from forms.models import FormTemplateVersion
from forms.validation import RecordValidationError

from .models import FormRecord, RecordStatus, SyncBatch
from .pull import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    InvalidCursor,
    decode_cursor,
    pull_changes,
)
from .serializers import ResolveConflictSerializer, SyncBatchRequestSerializer
from .services import delete_record, resolve_conflict, submit_batch


class BatchSubmitView(views.APIView):
    """POST /api/sync/submit/ -- offline batch upload (<= 50 entries).

    The response is always HTTP 200 at the envelope level (the batch itself
    was processed); each entry carries its own ``status_code`` (201/200/409/
    422/403/400). Retrying the same ``client_batch_id`` replays stored
    results verbatim.
    """

    def post(self, request):
        serializer = SyncBatchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch, outcomes, replayed = submit_batch(
            user=request.user,
            client_batch_id=serializer.validated_data["client_batch_id"],
            entries=serializer.validated_data["entries"],
            can_submit=can_submit_to_crew,
        )
        return Response(
            {
                "batch_id": batch.id,
                "client_batch_id": batch.client_batch_id,
                "replayed": replayed,
                "entries": [dict(o) for o in outcomes],
            },
            status=status.HTTP_200_OK,
        )


class BatchResultView(views.APIView):
    """GET /api/sync/batches/{client_batch_id}/ -- fetch retained results."""

    def get(self, request, client_batch_id):
        batch = get_object_or_404(
            SyncBatch.objects.prefetch_related("results").filter(
                submitted_by=request.user, client_batch_id=client_batch_id
            )
        )
        entries = [
            {
                "client_uuid": str(r.client_uuid),
                "status": r.status,
                "status_code": r.status_code,
                "record_version": r.record_version,
                "revision_seq": r.revision_seq,
                "content_hash": r.content_hash,
                "conflict_with_seq": r.conflict_with_seq,
                "errors": r.errors,
            }
            for r in batch.results.all()
        ]
        return Response(
            {
                "batch_id": batch.id,
                "client_batch_id": batch.client_batch_id,
                "created_at": batch.created_at.isoformat(),
                "entries": entries,
            }
        )


class PullView(views.APIView):
    """GET /api/sync/pull/?cursor=...&limit=... -- stable incremental feed."""

    def get(self, request):
        raw_cursor = request.query_params.get("cursor")
        try:
            limit = int(request.query_params.get("limit", DEFAULT_PAGE_LIMIT))
        except (TypeError, ValueError):
            return Response(
                {"detail": "limit must be an integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        limit = max(1, min(limit, MAX_PAGE_LIMIT))
        try:
            after_id = decode_cursor(raw_cursor)
        except InvalidCursor as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        project_id = request.query_params.get("project")
        template_id = request.query_params.get("template")
        if project_id and not project_id.isdigit():
            return Response({"detail": "project must be an id"}, status=400)
        if project_id and not can_access_project(request.user, int(project_id)):
            return Response({"detail": "forbidden"}, status=403)

        page = pull_changes(
            allowed_project_ids=list(accessible_project_ids(request.user)),
            allowed_crew_ids=list(accessible_crew_ids(request.user)),
            after_cursor=after_id,
            limit=limit,
            project_id=int(project_id) if project_id else None,
            template_id=int(template_id) if template_id and template_id.isdigit() else None,
            include_all=is_admin(request.user) and "scope=all" in request.query_params,
        )
        return Response(page)


class PendingConflictsView(views.APIView):
    """GET /api/sync/conflicts/ -- conflicts in crews the user supervises."""

    def get(self, request):
        if not is_supervisor(request.user):
            return Response({"detail": "supervisor role required"}, status=403)
        records = (
            FormRecord.objects.filter(status=RecordStatus.CONFLICT)
            .select_related("template", "current_version", "conflict_of", "crew", "project")
            .prefetch_related("revisions__template_version", "revisions__submitted_by")
        )
        if not is_admin(request.user):
            records = records.filter(crew_id__in=supervised_crew_ids(request.user))
        crew_id = request.query_params.get("crew")
        if crew_id and crew_id.isdigit():
            records = records.filter(crew_id=int(crew_id))

        return Response({"conflicts": [_serialize_conflict(r) for r in records]})


class ConflictResolveView(views.APIView):
    """POST /api/sync/conflicts/{uuid}/resolve/ -- supervisor merges."""

    def post(self, request, record_uuid):
        if not is_supervisor(request.user):
            return Response({"detail": "supervisor role required"}, status=403)
        record = get_object_or_404(FormRecord, record_uuid=record_uuid)
        if not is_admin(request.user) and not record.crew_id in list(
            supervised_crew_ids(request.user)
        ):
            return Response({"detail": "not a supervisor of this crew"}, status=403)
        if record.status != RecordStatus.CONFLICT:
            return Response({"detail": "record is not in conflict"}, status=409)

        serializer = ResolveConflictSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            record, revision = resolve_conflict(
                supervisor=request.user,
                record=record,
                merged_data=serializer.validated_data["merged_data"],
                template_version_no=serializer.validated_data["template_version"],
                note=serializer.validated_data["note"],
            )
        except FormTemplateVersion.DoesNotExist:
            return Response(
                {"detail": "template_version_unavailable"},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except RecordValidationError as exc:
            return Response(
                {"errors": exc.errors},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return Response(_serialize_conflict(record, resolved_revision=revision))


class RecordDetailView(views.APIView):
    """GET /api/sync/records/{uuid}/ -- full revision history of one record."""

    def get(self, request, record_uuid):
        record = get_object_or_404(FormRecord, record_uuid=record_uuid)
        visible_projects = list(accessible_project_ids(request.user))
        visible_crews = list(accessible_crew_ids(request.user))
        if not (
            is_admin(request.user)
            or (record.project_id in visible_projects and record.crew_id in visible_crews)
        ):
            return Response({"detail": "not found"}, status=404)

        revisions = record.revisions.select_related(
            "template_version", "submitted_by", "resolved_by"
        ).order_by("seq")
        return Response(
            {
                "uuid": str(record.record_uuid),
                "project_id": record.project_id,
                "crew_id": record.crew_id,
                "template_id": record.template_id,
                "status": record.status,
                "deleted": record.deleted,
                "current_revision_seq": record.current_revision_id
                and record.current_revision.seq,
                "revisions": [
                    {
                        "seq": rev.seq,
                        "kind": rev.kind,
                        "base_version": rev.base_version,
                        "template_version": rev.template_version.version,
                        "data": rev.data,
                        "content_hash": rev.content_hash,
                        "collected_at": rev.collected_at.isoformat(),
                        "submitted_by": rev.submitted_by_id,
                        "created_at": rev.created_at.isoformat(),
                        "resolved_by": rev.resolved_by_id,
                        "resolution_note": rev.resolution_note,
                    }
                    for rev in revisions
                ],
            }
        )


class RecordDeleteView(views.APIView):
    """DELETE /api/sync/records/{uuid}/ -- tombstone (supervisor/admin)."""

    def delete(self, request, record_uuid):
        if not is_supervisor(request.user):
            return Response({"detail": "supervisor role required"}, status=403)
        record = get_object_or_404(FormRecord, record_uuid=record_uuid)
        if not is_admin(request.user) and record.crew_id not in list(
            supervised_crew_ids(request.user)
        ):
            return Response({"detail": "not a supervisor of this crew"}, status=403)
        record, change = delete_record(user=request.user, record=record)
        return Response(
            {"uuid": str(record.record_uuid), "deleted": True, "status": record.status},
            status=status.HTTP_200_OK,
        )


def _serialize_conflict(record: FormRecord, *, resolved_revision=None):
    siblings = list(record.revisions.select_related("template_version", "submitted_by").order_by("seq"))
    payload = {
        "uuid": str(record.record_uuid),
        "project_id": record.project_id,
        "crew_id": record.crew_id,
        "template_id": record.template_id,
        "status": record.status,
        "conflict_of_seq": record.conflict_of.seq if record.conflict_of_id else None,
        "revisions": [
            {
                "seq": rev.seq,
                "kind": rev.kind,
                "base_version": rev.base_version,
                "template_version": rev.template_version.version,
                "data": rev.data,
                "content_hash": rev.content_hash,
                "collected_at": rev.collected_at.isoformat(),
                "submitted_by": rev.submitted_by_id,
                "created_at": rev.created_at.isoformat(),
                "resolved_by": rev.resolved_by_id,
                "resolution_note": rev.resolution_note,
            }
            for rev in siblings
        ],
    }
    if resolved_revision is not None:
        payload["resolved_revision_seq"] = resolved_revision.seq
    return payload
