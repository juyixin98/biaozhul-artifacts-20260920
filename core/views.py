"""HTTP 接口层。"""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Conflict,
    FormRecord,
    FormTemplate,
    Project,
    RecordVersion,
    SyncBatch,
    Team,
)
from .permissions import (
    IsAdminRole,
    can_access_project,
    can_manage_templates,
    can_resolve_project,
    user_project_ids,
)
from .serializers import (
    ConflictSerializer,
    FormRecordSerializer,
    FormTemplateSerializer,
    ProjectSerializer,
    ResolveConflictSerializer,
    SyncPushSerializer,
    TeamSerializer,
)
from .services import sync as sync_service
from .template_schema import (
    SchemaError,
    analyze_compatibility,
    validate_template_schema,
)


def _accessible_projects(user):
    return Project.objects.filter(id__in=user_project_ids(user))


class MeView(APIView):
    def get(self, request):
        u = request.user
        return Response(
            {
                "username": u.username,
                "role": u.profile.role,
                "accessible_projects": sorted(user_project_ids(u)),
                "supervised_team_ids": list(
                    u.supervised_teams.values_list("id", flat=True)
                ),
            }
        )


class ProjectViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectSerializer
    permission_classes = [IsAdminRole]

    def get_queryset(self):
        # 非管理员也需要列出项目（同步接口挂在 project 下），
        # 但仅管理员可写（permission_classes 控制）。
        if self.request.user.is_staff or self.request.user.profile.role == "admin":
            return Project.objects.all()
        return _accessible_projects(self.request.user)

    def get_permissions(self):
        if self.action in {"list", "retrieve", "sync_push", "sync_pull", "sync_batch"}:
            return [IsAuthenticated()]
        return [IsAdminRole()]

    # -- 批量推送 ----------------------------------------------------------- #
    @action(detail=True, methods=["post"], url_path="sync/push")
    def sync_push(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        if not can_access_project(request.user, project):
            return Response(
                {"detail": "您未被分配到该项目，禁止访问"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SyncPushSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        vd = serializer.validated_data
        # 信封只校验整体形状；单条字段级合法性由 process_item 逐条返回。
        raw_items = [dict(item) for item in vd["items"]]

        batch, result = sync_service.process_batch(
            request.user,
            project,
            vd["client_batch_id"],
            raw_items,
            device_id_default=vd.get("device_id", ""),
        )
        if batch is None:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        replayed = result is None
        results = sync_service.batch_results(batch) if replayed else result
        # 首次结果回填 uuid（重放结果由持久化的 raw_uuid 直接提供）
        if not replayed:
            by_index = {i: str(it["uuid"]) for i, it in enumerate(raw_items)}
            for idx, r in enumerate(results):
                r.setdefault("uuid", by_index.get(idx))

        item_errors = any(r["status"] == sync_service.ITEM_ERROR for r in results)
        http_status = (
            status.HTTP_207_MULTI_STATUS if item_errors else status.HTTP_200_OK
        )
        return Response(
            {
                "batch_id": batch.id,
                "client_batch_id": batch.client_batch_id,
                "replayed": replayed,
                "item_count": batch.item_count,
                "results": results,
            },
            status=http_status,
        )

    # -- 增量拉取 ----------------------------------------------------------- #
    @action(detail=True, methods=["get"], url_path="sync/pull")
    def sync_pull(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        if not can_access_project(request.user, project):
            return Response(
                {"detail": "您未被分配到该项目，禁止访问"},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            cursor = (
                int(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
            hwm = (
                int(request.query_params["hwm"])
                if "hwm" in request.query_params
                else None
            )
            limit = int(request.query_params.get("limit", "100"))
        except (TypeError, ValueError):
            return Response(
                {"detail": "cursor/hwm/limit 必须是整数"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = sync_service.pull_changes(
            request.user, project, cursor=cursor, limit=limit, hwm=hwm
        )
        return Response(data)

    # -- 批次对账（断网后找回首次结果） -------------------------------------- #
    @action(detail=True, methods=["get"], url_path="sync/batch")
    def sync_batch(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        if not can_access_project(request.user, project):
            return Response(
                {"detail": "您未被分配到该项目，禁止访问"},
                status=status.HTTP_403_FORBIDDEN,
            )
        cbid = request.query_params.get("client_batch_id")
        if not cbid:
            return Response(
                {"detail": "必须提供 client_batch_id"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        batch = get_object_or_404(
            SyncBatch, user=request.user, project=project, client_batch_id=cbid
        )
        return Response(
            {
                "batch_id": batch.id,
                "client_batch_id": batch.client_batch_id,
                "status": batch.status,
                "item_count": batch.item_count,
                "results": sync_service.batch_results(batch),
            }
        )


class TeamViewSet(viewsets.ModelViewSet):
    queryset = Team.objects.prefetch_related(
        "projects", "supervisors", "members"
    ).all()
    serializer_class = TeamSerializer
    permission_classes = [IsAdminRole]


class FormTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = FormTemplateSerializer

    def get_queryset(self):
        qs = FormTemplate.objects.select_related("project").filter(
            project_id__in=user_project_ids(self.request.user)
        )
        project = self.request.query_params.get("project")
        code = self.request.query_params.get("code")
        status_ = self.request.query_params.get("status")
        if project:
            qs = qs.filter(project_id=project)
        if code:
            qs = qs.filter(code=code)
        if status_:
            qs = qs.filter(status=status_)
        return qs.order_by("code", "-version")

    def _is_management_action(self):
        return self.action in {
            "create",
            "update",
            "partial_update",
            "destroy",
            "publish",
            "deprecate",
            "new_draft",
        }

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if self._is_management_action():
            project = None
            if self.action in {"update", "partial_update", "destroy", "publish",
                               "deprecate", "new_draft"}:
                # 管理动作的越权判定不能受可见范围过滤影响，否则会被隐藏成 404
                obj = FormTemplate.objects.select_related("project").filter(
                    pk=kwargs.get("pk")
                ).first()
                if obj is None:
                    return
                project = obj.project
            else:
                pid = request.data.get("project")
                if pid:
                    project = Project.objects.filter(id=pid).first()
            if not can_manage_templates(request.user, project):
                self.permission_denied(
                    request,
                    message="只有管理员或该项目所属班组的主管可以管理模板",
                )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data["project"]
        code = serializer.validated_data["code"]

        # 校验 schema 本身
        try:
            cleaned = validate_template_schema(serializer.validated_data["schema"])
        except SchemaError as exc:
            return Response(
                {"detail": "模板定义非法", "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 不允许对已存在 code 再用 POST：请用 /new_draft/ 派生草稿
        exists = FormTemplate.objects.filter(project=project, code=code).exists()
        if exists:
            return Response(
                {
                    "detail": f"模板 {code} 已存在版本；"
                    "请基于最新版本调用 new_draft/ 创建下一版草稿"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        template = FormTemplate(
            project=project,
            code=code,
            version=1,
            name=serializer.validated_data["name"],
            schema={"fields": cleaned,
                    "version_notes": serializer.validated_data.get("version_notes", "")},
            version_notes=serializer.validated_data.get("version_notes", ""),
            created_by=request.user,
            status=FormTemplate.Status.DRAFT,
        )
        template.save()
        return Response(
            FormTemplateSerializer(template).data,
            status=status.HTTP_201_CREATED,
        )

    def _block_published_mutation(self, template):
        if template.status != FormTemplate.Status.DRAFT:
            return Response(
                {"detail": "已发布/已弃用的版本不可修改（发布版本不可变）"},
                status=status.HTTP_409_CONFLICT,
            )
        return None

    def update(self, request, *args, **kwargs):
        template = self.get_object()
        blocked = self._block_published_mutation(template)
        if blocked:
            return blocked
        serializer = self.get_serializer(
            template, data=request.data, partial=kwargs.get("partial", False)
        )
        serializer.is_valid(raise_exception=True)
        schema = serializer.validated_data.get("schema", template.schema)
        try:
            cleaned = validate_template_schema(schema)
        except SchemaError as exc:
            return Response(
                {"detail": "模板定义非法", "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        template.name = serializer.validated_data.get("name", template.name)
        template.schema = {"fields": cleaned}
        if "version_notes" in serializer.validated_data:
            template.version_notes = serializer.validated_data["version_notes"]
        template.save()
        return Response(FormTemplateSerializer(template).data)

    def destroy(self, request, *args, **kwargs):
        template = self.get_object()
        blocked = self._block_published_mutation(template)
        if blocked:
            return blocked
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def new_draft(self, request, pk=None):
        """基于某版本创建下一版草稿（深拷贝 schema）。"""
        source = self.get_object()
        project = source.project
        with transaction.atomic():
            latest = (
                FormTemplate.objects.select_for_update()
                .filter(project=project, code=source.code)
                .order_by("-version")
                .first()
            )
            draft_exists = FormTemplate.objects.filter(
                project=project, code=source.code, status=FormTemplate.Status.DRAFT
            ).exists()
            if draft_exists:
                return Response(
                    {"detail": "该模板已存在草稿，请先发布或删除草稿"},
                    status=status.HTTP_409_CONFLICT,
                )
            new_version = latest.version + 1
            draft = FormTemplate.objects.create(
                project=project,
                code=source.code,
                version=new_version,
                name=source.name,
                schema={"fields": list(source.field_defs)},
                version_notes=request.data.get("version_notes", ""),
                created_by=request.user,
                status=FormTemplate.Status.DRAFT,
            )
        return Response(
            FormTemplateSerializer(draft).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        """发布草稿：执行兼容性分析；发布后行不可变。"""
        template = self.get_object()
        if template.status != FormTemplate.Status.DRAFT:
            return Response(
                {"detail": "只有草稿可以发布"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            cleaned = validate_template_schema(template.schema)
        except SchemaError as exc:
            return Response(
                {"detail": "模板定义非法", "errors": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        previous = (
            FormTemplate.objects.filter(
                project=template.project,
                code=template.code,
                status=FormTemplate.Status.PUBLISHED,
            )
            .order_by("-version")
            .first()
        )
        old_fields = previous.field_defs if previous else []
        legacy_fields, warnings, blocking = analyze_compatibility(
            old_fields, cleaned
        )
        if blocking:
            return Response(
                {
                    "detail": "存在破坏性兼容问题，发布被拒绝；请调整草稿后重试",
                    "blocking": blocking,
                    "warnings": warnings,
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            template.status = FormTemplate.Status.PUBLISHED
            template.published_at = timezone.now()
            template.schema = {"fields": cleaned}
            template.legacy_fields = legacy_fields
            template.save(
                update_fields=[
                    "status",
                    "published_at",
                    "schema",
                    "legacy_fields",
                ]
            )
            # 旧发布版本标记弃用（历史提交仍可按其版本校验/读取）
            FormTemplate.objects.filter(
                project=template.project,
                code=template.code,
                status=FormTemplate.Status.PUBLISHED,
            ).exclude(id=template.id).update(
                status=FormTemplate.Status.DEPRECATED,
                deprecated_at=timezone.now(),
            )
        return Response(
            {
                "template": FormTemplateSerializer(template).data,
                "compatibility": {
                    "legacy_fields": legacy_fields,
                    "warnings": warnings,
                    "blocking": blocking,
                },
            }
        )

    @action(detail=True, methods=["post"])
    def deprecate(self, request, pk=None):
        template = self.get_object()
        if template.status != FormTemplate.Status.PUBLISHED:
            return Response(
                {"detail": "只有已发布版本可以弃用"},
                status=status.HTTP_409_CONFLICT,
            )
        template.status = FormTemplate.Status.DEPRECATED
        template.deprecated_at = timezone.now()
        template.save(update_fields=["status", "deprecated_at"])
        return Response(FormTemplateSerializer(template).data)


class FormRecordViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = FormRecordSerializer
    lookup_field = "uuid"

    def get_queryset(self):
        qs = FormRecord.objects.filter(
            project_id__in=user_project_ids(self.request.user)
        ).select_related("project", "template", "current_version")
        project = self.request.query_params.get("project")
        if project:
            qs = qs.filter(project_id=project)
        status_ = self.request.query_params.get("status")
        if status_:
            qs = qs.filter(status=status_)
        return qs.order_by("-created_at", "-id")

    @action(detail=True, methods=["post"], url_path="delete")
    def mark_deleted(self, request, uuid=None):
        record = self.get_object()
        if not can_resolve_project(request.user, record.project):
            return Response(
                {"detail": "只有该项目所属班组的主管可以删除记录"},
                status=status.HTTP_403_FORBIDDEN,
            )
        record, result = sync_service.mark_record_deleted(request.user, record)
        if result == "conflict_open":
            return Response(
                {"detail": "记录存在未解决冲突，请先解决冲突再删除"},
                status=status.HTTP_409_CONFLICT,
            )
        if result is False:
            return Response(
                {"detail": "记录已处于删除状态", "status": record.status},
                status=status.HTTP_200_OK,
            )
        return Response(
            {"detail": "已打删除标记", "status": record.status},
            status=status.HTTP_200_OK,
        )


class ConflictViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ConflictSerializer

    def get_queryset(self):
        qs = Conflict.objects.filter(
            project_id__in=user_project_ids(self.request.user)
        ).prefetch_related("versions")
        project = self.request.query_params.get("project")
        if project:
            qs = qs.filter(project_id=project)
        status_filter = self.request.query_params.get("status", "open")
        if status_filter != "all":
            qs = qs.filter(status=status_filter)
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        # 不按可见范围过滤：先找到冲突，再明确判定主管是否属于该班组，
        # 越权时返回 403（而不是被隐藏成 404）。
        conflict = get_object_or_404(Conflict, pk=pk)
        if not can_resolve_project(request.user, conflict.project):
            return Response(
                {"detail": "只有该冲突所属班组的主管可以解决冲突"},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = ResolveConflictSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        winning = RecordVersion.objects.filter(
            id=serializer.validated_data["winning_version_id"],
            record=conflict.record,
        ).first()
        if winning is None:
            return Response(
                {"detail": "winning_version_id 不属于该冲突记录"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        conflict, err = sync_service.resolve_conflict(
            conflict,
            winning,
            serializer.validated_data["note"],
            request.user,
        )
        if err:
            return Response(err, status=status.HTTP_409_CONFLICT)
        return Response(ConflictSerializer(conflict).data)
