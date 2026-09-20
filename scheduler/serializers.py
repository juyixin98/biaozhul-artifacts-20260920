from rest_framework import serializers

from .models import (
    Allocation,
    Job,
    Node,
    PreemptionRecord,
    ResourcePool,
    SchedulingDecision,
)


class ResourcePoolSerializer(serializers.ModelSerializer):
    used_gpus = serializers.SerializerMethodField()
    active_jobs = serializers.SerializerMethodField()

    class Meta:
        model = ResourcePool
        fields = [
            "id", "name", "max_gpus", "max_running_jobs",
            "used_gpus", "active_jobs", "created_at",
        ]

    def get_used_gpus(self, obj) -> int:
        from django.db.models import Sum

        return (
            Allocation.objects.filter(node__pool=obj).aggregate(t=Sum("gpu_count"))["t"]
            or 0
        )

    def get_active_jobs(self, obj) -> int:
        return Job.objects.filter(pool=obj, status__in=Job.ACTIVE_STATUSES).count()


class NodeSerializer(serializers.ModelSerializer):
    used_gpus = serializers.SerializerMethodField()
    free_gpus = serializers.SerializerMethodField()

    class Meta:
        model = Node
        fields = [
            "id", "pool", "name", "gpu_count", "gpu_memory_mb",
            "status", "last_heartbeat_at", "used_gpus", "free_gpus",
            "created_at", "updated_at",
        ]
        read_only_fields = ["status", "last_heartbeat_at"]

    def get_used_gpus(self, obj) -> int:
        from django.db.models import Sum

        return (
            Allocation.objects.filter(node=obj).aggregate(t=Sum("gpu_count"))["t"]
            or 0
        )

    def get_free_gpus(self, obj) -> int:
        used = self.get_used_gpus(obj)
        return max(obj.gpu_count - used, 0)


class HeartbeatSerializer(serializers.Serializer):
    gpu_count = serializers.IntegerField(min_value=0, required=False)
    gpu_memory_mb = serializers.IntegerField(min_value=0, required=False)


class JobSerializer(serializers.ModelSerializer):
    allocations = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            "id", "pool", "name", "min_gpus", "max_gpus", "gpu_memory_mb",
            "priority", "status", "status_reason", "run_timeout_seconds",
            "queued_at", "allocated_at", "started_at", "finished_at",
            "created_at", "updated_at", "allocations",
        ]
        read_only_fields = [
            "status", "status_reason", "queued_at", "allocated_at",
            "started_at", "finished_at",
        ]

    def get_allocations(self, obj):
        return [
            {"node_id": a.node_id, "gpu_count": a.gpu_count}
            for a in obj.allocations.all()
        ]

    def validate_priority(self, value: int) -> int:
        if not 1 <= value <= 10:
            raise serializers.ValidationError("优先级必须在 1—10 之间")
        return value

    def validate(self, attrs):
        mn = attrs.get("min_gpus")
        mx = attrs.get("max_gpus")
        if mn is not None and mn < 1:
            raise serializers.ValidationError("min_gpus 至少为 1")
        if mn is not None and mx is not None and mn > mx:
            raise serializers.ValidationError("min_gpus 不能大于 max_gpus")
        return attrs


class SchedulingDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchedulingDecision
        fields = ["id", "pool", "job", "node", "kind", "rationale",
                  "success", "created_at"]


class PreemptionRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreemptionRecord
        fields = ["id", "preemptor", "victim", "pool", "success",
                  "detail", "created_at"]
