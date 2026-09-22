"""REST API for the simulated GPU scheduling engine."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .clock import Clock
from .engine import tick
from .models import (
    Allocation,
    Job,
    Node,
    NodeAdminState,
    PreemptionRequest,
    ResourcePool,
    SchedulingDecision,
)
from .serializers import (
    AllocationSerializer,
    HeartbeatSerializer,
    JobSerializer,
    NodeRegisterSerializer,
    NodeSerializer,
    PreemptionRequestSerializer,
    ResourcePoolSerializer,
    SchedulingDecisionSerializer,
)
from .services import SchedulerError


class APIRootView(APIView):
    """Tiny index so hitting '/' lists the available endpoints."""

    def get(self, request):
        return Response(
            {
                "pools": "/api/pools/",
                "nodes": "/api/nodes/",
                "jobs": "/api/jobs/",
                "allocations": "/api/allocations/",
                "preemptions": "/api/preemptions/",
                "decisions": "/api/decisions/ (audit trail)",
                "register": "/api/nodes/register/",
                "heartbeat": "/api/nodes/heartbeat/",
                "scheduler_tick": "/api/scheduler/tick/",
            }
        )


def _error(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

class PoolListCreateView(APIView):
    def get(self, request):
        return Response(
            ResourcePoolSerializer(
                ResourcePool.objects.all().order_by("id"), many=True
            ).data
        )

    def post(self, request):
        ser = ResourcePoolSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        pool = services.create_pool(
            name=ser.validated_data["name"],
            max_total_gpus=ser.validated_data["max_total_gpus"],
            max_concurrent_jobs=ser.validated_data["max_concurrent_jobs"],
            description=ser.validated_data.get("description", ""),
        )
        return Response(
            ResourcePoolSerializer(pool).data, status=status.HTTP_201_CREATED
        )


class PoolDetailView(APIView):
    def get(self, request, pk):
        pool = ResourcePool.objects.filter(pk=pk).first()
        if pool is None:
            return Response({"detail": "not found"}, status=404)
        return Response(ResourcePoolSerializer(pool).data)

    def patch(self, request, pk):
        pool = ResourcePool.objects.filter(pk=pk).first()
        if pool is None:
            return Response({"detail": "not found"}, status=404)
        try:
            pool = services.update_pool(
                pool,
                max_total_gpus=request.data.get("max_total_gpus"),
                max_concurrent_jobs=request.data.get("max_concurrent_jobs"),
            )
        except SchedulerError as exc:
            return _error(exc)
        return Response(ResourcePoolSerializer(pool).data)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

class NodeListCreateView(APIView):
    def get(self, request):
        qs = Node.objects.select_related("pool").all().order_by("id")
        pool = request.query_params.get("pool")
        if pool:
            qs = qs.filter(pool__name=pool)
        return Response(NodeSerializer(qs, many=True).data)


class NodeRegisterView(APIView):
    """Simulated nodes call this to (re)report hardware and identify pool."""

    def post(self, request):
        ser = NodeRegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        pool = ResourcePool.objects.filter(name=d["pool"]).first()
        if pool is None:
            return Response(
                {"detail": f"unknown pool '{d['pool']}'"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            node = services.register_node(
                pool,
                d["hostname"],
                d["gpu_count"],
                d["gpu_memory_mb"],
            )
        except SchedulerError as exc:
            return _error(exc)
        return Response(NodeSerializer(node).data, status=status.HTTP_201_CREATED)


class NodeHeartbeatView(APIView):
    def post(self, request):
        ser = HeartbeatSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        try:
            node = services.heartbeat(
                d["hostname"],
                Clock().now(),
                reported_gpus=d.get("gpu_count"),
                reported_memory_mb=d.get("gpu_memory_mb"),
            )
        except SchedulerError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(NodeSerializer(node).data)


class NodeAdminStateView(APIView):
    """Drain / online / offline an existing node."""

    def post(self, request, pk):
        node = Node.objects.filter(pk=pk).first()
        if node is None:
            return Response({"detail": "not found"}, status=404)
        new_state = request.data.get("admin_state")
        if new_state not in NodeAdminState.values:
            return Response(
                {
                    "detail": f"admin_state must be one of {NodeAdminState.values}"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            node = services.set_admin_state(
                node, NodeAdminState(new_state), Clock().now()
            )
        except SchedulerError as exc:
            return _error(exc)
        return Response(NodeSerializer(node).data)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class JobListCreateView(APIView):
    def get(self, request):
        qs = Job.objects.select_related("pool").all().order_by("id")
        pool = request.query_params.get("pool")
        state = request.query_params.get("state")
        if pool:
            qs = qs.filter(pool__name=pool)
        if state:
            qs = qs.filter(state=state)
        return Response(JobSerializer(qs, many=True).data)

    def post(self, request):
        ser = JobSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        pool_id = ser.validated_data["pool"].pk
        pool = ResourcePool.objects.filter(pk=pool_id).first()
        job = services.create_job(
            pool,
            name=ser.validated_data["name"],
            min_gpus=ser.validated_data["min_gpus"],
            max_gpus=ser.validated_data["max_gpus"],
            gpu_memory_mb=ser.validated_data["gpu_memory_mb"],
            priority=ser.validated_data["priority"],
        )
        return Response(JobSerializer(job).data, status=status.HTTP_201_CREATED)


class JobDetailView(APIView):
    def get(self, request, pk):
        job = Job.objects.filter(pk=pk).first()
        if job is None:
            return Response({"detail": "not found"}, status=404)
        return Response(JobSerializer(job).data)


class JobCancelView(APIView):
    def post(self, request, pk):
        job = Job.objects.filter(pk=pk).first()
        if job is None:
            return Response({"detail": "not found"}, status=404)
        won, job = services.cancel_job(job, Clock().now())
        if not won:
            return Response(
                {"detail": f"job already settled ({job.state}/{job.finish_reason})"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(JobSerializer(job).data)


class JobCompleteView(APIView):
    """Worker reports a normal completion and releases GPUs."""

    def post(self, request, pk):
        job = Job.objects.filter(pk=pk).first()
        if job is None:
            return Response({"detail": "not found"}, status=404)
        won, job = services.report_completion(job, Clock().now())
        if not won:
            return Response(
                {"detail": f"job already settled ({job.state}/{job.finish_reason})"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(JobSerializer(job).data)


class JobReleasePreemptedView(APIView):
    """Worker acknowledges preemption and has released its GPUs."""

    def post(self, request, pk):
        job = Job.objects.filter(pk=pk).first()
        if job is None:
            return Response({"detail": "not found"}, status=404)
        won, job = services.report_preempted_release(job, Clock().now())
        if not won:
            return Response(
                {"detail": f"job not in PREEMPTING (is {job.state})"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(JobSerializer(job).data)


# ---------------------------------------------------------------------------
# Allocations / preemptions / decisions
# ---------------------------------------------------------------------------

class AllocationListView(APIView):
    def get(self, request):
        qs = Allocation.objects.select_related("job", "node", "pool").order_by("id")
        pool = request.query_params.get("pool")
        node = request.query_params.get("node")
        if pool:
            qs = qs.filter(pool__name=pool)
        if node:
            qs = qs.filter(node__hostname=node)
        return Response(AllocationSerializer(qs, many=True).data)


class PreemptionListView(APIView):
    def get(self, request):
        qs = (
            PreemptionRequest.objects.select_related(
                "beneficiary", "target_node", "pool"
            )
            .prefetch_related("victim_rows__job")
            .order_by("-id")
        )
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        return Response(PreemptionRequestSerializer(qs, many=True).data)


class DecisionListView(APIView):
    """Audit trail of every scheduling/preemption basis."""

    def get(self, request):
        qs = SchedulingDecision.objects.select_related(
            "pool", "job", "node", "preemption"
        ).order_by("-id")
        kind = request.query_params.get("kind")
        job = request.query_params.get("job")
        if kind:
            qs = qs.filter(kind=kind)
        if job:
            qs = qs.filter(job_id=job)
        limit = min(int(request.query_params.get("limit", 200)), 1000)
        return Response(
            SchedulingDecisionSerializer(qs[:limit], many=True).data
        )


# ---------------------------------------------------------------------------
# Manual scheduler tick (tests / demos / single-shot runs)
# ---------------------------------------------------------------------------

class SchedulerTickView(APIView):
    def post(self, request):
        return Response(tick(Clock()))
