from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import Max
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Case,
    ProcessTemplate,
    Project,
    TemplateVersion,
)
from .permissions import get_authorized_project
from .services import BatchImportError, import_events, rebuild_project
from .template_def import validate_definition


def _validation_response(exc):
    return Response({"errors": exc.messages}, status=status.HTTP_400_BAD_REQUEST)


def template_version_json(v):
    return {
        "id": v.id,
        "template_id": v.template_id,
        "version": v.version,
        "status": v.status,
        "definition": v.definition,
        "created_at": v.created_at,
        "published_at": v.published_at,
    }


def analysis_json(a):
    return {
        "revision": a.revision,
        "status": a.status,
        "missing_seqs": a.missing_seqs,
        "deviations": a.deviations,
        "event_count": a.event_count,
        "is_current": a.is_current,
        "created_at": a.created_at,
    }


def case_json(case, with_analysis=True):
    data = {
        "case_key": case.case_key,
        "template_version_id": case.template_version_id,
        "created_at": case.created_at,
    }
    if with_analysis:
        current = case.analyses.filter(is_current=True).first()
        data["analysis"] = analysis_json(current) if current else None
    return data


class ProjectListView(APIView):
    """List only the projects the analyst is authorized for."""

    def get(self, request):
        qs = Project.objects.all()
        if not request.user.is_staff:
            qs = qs.filter(memberships__user=request.user)
        qs = qs.distinct().order_by("id")
        return Response(
            [{"id": p.id, "name": p.name, "created_at": p.created_at} for p in qs]
        )


class TemplateListCreateView(APIView):
    def get(self, request, project_id):
        project = get_authorized_project(request, project_id)
        data = []
        for tpl in project.templates.prefetch_related("versions"):
            data.append(
                {
                    "id": tpl.id,
                    "name": tpl.name,
                    "versions": [
                        template_version_json(v) for v in tpl.versions.all()
                    ],
                }
            )
        return Response(data)

    def post(self, request, project_id):
        """Create a template together with draft version 1."""
        project = get_authorized_project(request, project_id)
        name = request.data.get("name")
        definition = request.data.get("definition")
        if not name or not isinstance(name, str):
            return Response(
                {"errors": ["'name' is required"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            normalized = validate_definition(definition)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        try:
            tpl = ProcessTemplate.objects.create(project=project, name=name)
        except IntegrityError:
            return Response(
                {"errors": [f"template '{name}' already exists in this project"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        version = TemplateVersion.objects.create(
            template=tpl, version=1, definition=normalized
        )
        return Response(
            {"id": tpl.id, "name": tpl.name, "draft": template_version_json(version)},
            status=status.HTTP_201_CREATED,
        )


class VersionListCreateView(APIView):
    def get(self, request, template_id):
        tpl = get_object_or_404(ProcessTemplate, pk=template_id)
        get_authorized_project(request, tpl.project_id)
        return Response(
            [template_version_json(v) for v in tpl.versions.all()]
        )

    def post(self, request, template_id):
        """Create the next draft version; published versions stay immutable."""
        tpl = get_object_or_404(ProcessTemplate, pk=template_id)
        get_authorized_project(request, tpl.project_id)
        try:
            normalized = validate_definition(request.data.get("definition"))
        except DjangoValidationError as exc:
            return _validation_response(exc)
        next_version = (tpl.versions.aggregate(m=Max("version"))["m"] or 0) + 1
        version = TemplateVersion.objects.create(
            template=tpl, version=next_version, definition=normalized
        )
        return Response(template_version_json(version), status=status.HTTP_201_CREATED)


class VersionPublishView(APIView):
    def post(self, request, version_id):
        version = get_object_or_404(TemplateVersion, pk=version_id)
        get_authorized_project(request, version.template.project_id)
        if version.status == TemplateVersion.Status.PUBLISHED:
            return Response(
                {"errors": ["version is already published"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            version.definition = validate_definition(version.definition)
            version.status = TemplateVersion.Status.PUBLISHED
            version.published_at = timezone.now()
            version.save()
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response(template_version_json(version))


class CaseListCreateView(APIView):
    def get(self, request, project_id):
        project = get_authorized_project(request, project_id)
        return Response(
            [case_json(c) for c in project.cases.prefetch_related("analyses")]
        )

    def post(self, request, project_id):
        """Bind a case key to a published template version."""
        project = get_authorized_project(request, project_id)
        case_key = request.data.get("case_key")
        version_id = request.data.get("template_version_id")
        version = get_object_or_404(
            TemplateVersion, pk=version_id, template__project=project
        )
        if version.status != TemplateVersion.Status.PUBLISHED:
            return Response(
                {"errors": ["cases can only bind to published template versions"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not case_key:
            return Response(
                {"errors": ["'case_key' is required"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        case, created = Case.objects.get_or_create(
            project=project,
            case_key=case_key,
            defaults={"template_version": version},
        )
        if not created and case.template_version_id != version.id:
            return Response(
                {
                    "errors": [
                        f"case '{case_key}' is already bound to template "
                        f"version {case.template_version_id}"
                    ]
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            case_json(case),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class EventImportView(APIView):
    """Atomic batch import of case events.

    Accepts {"events": [...]} or a bare list. The whole batch is validated
    before anything is written; any conflict rejects the entire batch.
    """

    def post(self, request, project_id):
        project = get_authorized_project(request, project_id)
        items = request.data
        if isinstance(items, dict):
            items = items.get("events")
        try:
            batch = import_events(project, items)
        except BatchImportError as exc:
            return Response(
                {"errors": exc.errors}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            {
                "batch_id": batch.id,
                "status": batch.status,
                "total": batch.total,
                "inserted": batch.inserted,
                "skipped": batch.skipped,
            },
            status=status.HTTP_201_CREATED,
        )


def _get_case(request, project_id, case_key):
    project = get_authorized_project(request, project_id)
    return get_object_or_404(Case, project=project, case_key=case_key)


class CaseTraceView(APIView):
    """Full ordered trace of a case with its template binding."""

    def get(self, request, project_id, case_key):
        case = _get_case(request, project_id, case_key)
        events = [
            {
                "event_id": e.event_id,
                "activity": e.activity,
                "occurred_at": e.occurred_at,
                "seq": e.seq,
            }
            for e in case.events.order_by("seq", "id")
        ]
        current = case.analyses.filter(is_current=True).first()
        return Response(
            {
                "case_key": case.case_key,
                "template_version": template_version_json(case.template_version),
                "status": current.status if current else "pending",
                "events": events,
            }
        )


class CaseDeviationView(APIView):
    """Current deviation list for one case, with evidence and constraints."""

    def get(self, request, project_id, case_key):
        case = _get_case(request, project_id, case_key)
        current = case.analyses.filter(is_current=True).first()
        if current is None:
            return Response(
                {"case_key": case.case_key, "status": "pending", "deviations": []}
            )
        return Response(
            {
                "case_key": case.case_key,
                "status": current.status,
                "missing_seqs": current.missing_seqs,
                "revision": current.revision,
                "deviations": current.deviations,
            }
        )


class CaseAnalysisHistoryView(APIView):
    """All analysis revisions of a case; superseded conclusions are kept."""

    def get(self, request, project_id, case_key):
        case = _get_case(request, project_id, case_key)
        return Response(
            [analysis_json(a) for a in case.analyses.order_by("-revision")]
        )


class ProjectDeviationReportView(APIView):
    """Per-case deviation report for the project.

    Deliberately returns the concrete deviation lists instead of a single
    aggregate score, so individual problems stay visible.
    """

    def get(self, request, project_id):
        project = get_authorized_project(request, project_id)
        deviation_type = request.query_params.get("type")
        only_deviating = request.query_params.get("deviating") == "1"
        cases = []
        for case in project.cases.prefetch_related("analyses").order_by("case_key"):
            current = case.analyses.filter(is_current=True).first()
            if current is None:
                continue
            deviations = current.deviations
            if deviation_type:
                deviations = [
                    d for d in deviations if d.get("type") == deviation_type
                ]
            if only_deviating and not deviations and not current.missing_seqs:
                continue
            cases.append(
                {
                    "case_key": case.case_key,
                    "template_version_id": case.template_version_id,
                    "status": current.status,
                    "missing_seqs": current.missing_seqs,
                    "deviations": deviations,
                }
            )
        return Response({"project": project.name, "cases": cases})


class RebuildView(APIView):
    """Full rebuild of every case analysis in the project."""

    def post(self, request, project_id):
        project = get_authorized_project(request, project_id)
        run = rebuild_project(project)
        return Response(
            {
                "run_id": run.id,
                "kind": run.kind,
                "cases_processed": run.cases_processed,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
            }
        )
