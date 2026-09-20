"""HTTP API：资源池/节点/作业管理 + 调度触发 + 审计查询。"""
from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import engine, reconcile
from .clock import get_clock
from .models import Job, Node, PreemptionRecord, ResourcePool, SchedulingDecision
from .serializers import (
    HeartbeatSerializer,
    JobSerializer,
    NodeSerializer,
    PreemptionRecordSerializer,
    ResourcePoolSerializer,
    SchedulingDecisionSerializer,
)


class ResourcePoolViewSet(viewsets.ModelViewSet):
    queryset = ResourcePool.objects.all().order_by("id")
    serializer_class = ResourcePoolSerializer

    @action(detail=True, methods=["post"])
    def schedule(self, request, pk=None):
        """触发该资源池一轮调度。"""
        pool = self.get_object()
        result = engine.schedule_pool(pool.id)
        if result is None:
            return Response(
                {"detail": "其它调度器正持有该资源池锁，本轮跳过"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(result.as_dict())

    @action(detail=False, methods=["post"], url_path="schedule-all")
    def schedule_all(self, request):
        results = engine.schedule_all_pools()
        return Response([r.as_dict() for r in results])


class NodeViewSet(viewsets.ModelViewSet):
    queryset = Node.objects.select_related("pool").all().order_by("id")
    serializer_class = NodeSerializer

    @action(detail=True, methods=["post"])
    def heartbeat(self, request, pk=None):
        """节点上报心跳（可携带最新 GPU 数量与单卡显存）。"""
        node = self.get_object()
        serializer = HeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        node = reconcile.heartbeat(
            node.id,
            gpu_count=serializer.validated_data.get("gpu_count"),
            gpu_memory_mb=serializer.validated_data.get("gpu_memory_mb"),
        )
        return Response(NodeSerializer(node).data)

    @action(detail=True, methods=["post"])
    def drain(self, request, pk=None):
        """排空节点：不再接收新任务，存量作业自然结束。"""
        node = reconcile.mark_draining(self.get_object().id)
        return Response(NodeSerializer(node).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """解除排空/离线隔离，节点重新可调度。"""
        node = reconcile.activate_node(self.get_object().id)
        return Response(NodeSerializer(node).data)

    @action(detail=False, methods=["post"], url_path="detect-stale")
    def detect_stale(self, request):
        """主动执行一次心跳失联检测（后台调度循环也会周期执行）。"""
        stale = reconcile.detect_stale_nodes()
        return Response({"offline_node_ids": stale})


class JobViewSet(viewsets.ModelViewSet):
    queryset = Job.objects.select_related("pool").prefetch_related(
        "allocations"
    ).all().order_by("id")
    serializer_class = JobSerializer

    def perform_create(self, serializer):
        job = serializer.save(status=Job.Status.QUEUED, queued_at=get_clock().now())
        return job

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job = self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(
            JobSerializer(job).data,
            status=status.HTTP_201_CREATED, headers=headers,
        )

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        """模拟训练器拿到资源后开始运行：ALLOCATED -> RUNNING。"""
        with transaction.atomic():
            job = get_object_or_404(
                Job.objects.select_for_update(), pk=pk
            )
            if job.status != Job.Status.ALLOCATED:
                return Response(
                    {"detail": f"作业当前状态 {job.status} 不可启动"},
                    status=status.HTTP_409_CONFLICT,
                )
            job.status = Job.Status.RUNNING
            job.started_at = get_clock().now()
            job.save(update_fields=["status", "started_at", "updated_at"])
        return Response(JobSerializer(job).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        ok = reconcile.complete_job(int(pk))
        return self._settle_response(ok, "completed")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        ok = reconcile.cancel_job(int(pk))
        return self._settle_response(ok, "cancelled")

    def _settle_response(self, ok: bool, verb: str):
        job = self.get_object()
        if not ok:
            return Response(
                {"detail": f"作业已是终态 {job.status}，结算幂等跳过（资源不会重复释放）"},
                status=status.HTTP_409_CONFLICT,
            )
        job.refresh_from_db()
        return Response(JobSerializer(job).data)


class SchedulingDecisionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SchedulingDecision.objects.select_related(
        "pool", "job", "node"
    ).all().order_by("-id")
    serializer_class = SchedulingDecisionSerializer
    filterset_fields = ["pool", "job", "kind", "success"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        for field in ("pool", "job", "node", "kind", "success"):
            value = params.get(field)
            if value is not None:
                qs = qs.filter(**{field: value})
        return qs


class PreemptionRecordViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = PreemptionRecord.objects.select_related(
        "preemptor", "victim", "pool"
    ).all().order_by("-id")
    serializer_class = PreemptionRecordSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        pool = self.request.query_params.get("pool")
        if pool is not None:
            qs = qs.filter(pool=pool)
        return qs
