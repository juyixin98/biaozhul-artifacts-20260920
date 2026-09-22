from rest_framework import serializers

from .models import (
    Allocation,
    Job,
    JobState,
    Node,
    NodeAdminState,
    PreemptionRequest,
    PreemptionVictim,
    ResourcePool,
    SchedulingDecision,
)


class ResourcePoolSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourcePool
        fields = [
            "id",
            "name",
            "description",
            "max_total_gpus",
            "max_concurrent_jobs",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class NodeSerializer(serializers.ModelSerializer):
    # Effective state is derived from admin state + heartbeat freshness.
    state = serializers.SerializerMethodField()
    free_gpus = serializers.IntegerField(read_only=True)

    class Meta:
        model = Node
        fields = [
            "id",
            "pool",
            "hostname",
            "gpu_count",
            "gpu_memory_mb",
            "admin_state",
            "state",
            "free_gpus",
            "allocated_gpus",
            "registered_at",
            "last_heartbeat_at",
            "marked_offline_at",
        ]
        read_only_fields = [
            "id",
            "state",
            "free_gpus",
            "allocated_gpus",
            "registered_at",
            "last_heartbeat_at",
            "marked_offline_at",
        ]

    def get_state(self, obj):
        from django.conf import settings

        from .clock import Clock

        timeout = settings.SCHEDULER["HEARTBEAT_TIMEOUT_SECONDS"]
        return obj.effective_state(Clock().now(), timeout).value


class NodeRegisterSerializer(serializers.Serializer):
    hostname = serializers.CharField(max_length=128)
    pool = serializers.CharField(max_length=64)
    gpu_count = serializers.IntegerField(min_value=1)
    gpu_memory_mb = serializers.IntegerField(min_value=1)


class HeartbeatSerializer(serializers.Serializer):
    hostname = serializers.CharField(max_length=128)
    gpu_count = serializers.IntegerField(min_value=1, required=False)
    gpu_memory_mb = serializers.IntegerField(min_value=1, required=False)


class JobSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = [
            "id",
            "pool",
            "name",
            "min_gpus",
            "max_gpus",
            "gpu_memory_mb",
            "priority",
            "state",
            "finish_reason",
            "enqueued_at",
            "scheduled_at",
            "finished_at",
        ]
        read_only_fields = [
            "id",
            "state",
            "finish_reason",
            "enqueued_at",
            "scheduled_at",
            "finished_at",
        ]

    def validate(self, attrs):
        mn = attrs.get("min_gpus")
        mx = attrs.get("max_gpus")
        if mn is not None and mn < 1:
            raise serializers.ValidationError("min_gpus must be >= 1")
        if mx is not None and mn is not None and mx < mn:
            raise serializers.ValidationError(
                "max_gpus must be >= min_gpus"
            )
        prio = attrs.get("priority")
        if prio is not None and not (1 <= prio <= 10):
            raise serializers.ValidationError("priority must be between 1 and 10")
        return attrs


class AllocationSerializer(serializers.ModelSerializer):
    hostname = serializers.CharField(source="node.hostname", read_only=True)
    job_name = serializers.CharField(source="job.name", read_only=True)

    class Meta:
        model = Allocation
        fields = [
            "id",
            "job",
            "job_name",
            "node",
            "hostname",
            "pool",
            "gpu_count",
            "placed_at",
        ]
        read_only_fields = fields


class PreemptionVictimSerializer(serializers.ModelSerializer):
    job_name = serializers.CharField(source="job.name", read_only=True)

    class Meta:
        model = PreemptionVictim
        fields = ["job", "job_name", "gpu_count"]


class PreemptionRequestSerializer(serializers.ModelSerializer):
    victims = PreemptionVictimSerializer(
        source="victim_rows", many=True, read_only=True
    )
    beneficiary_name = serializers.CharField(
        source="beneficiary.name", read_only=True
    )
    target_hostname = serializers.CharField(
        source="target_node.hostname", read_only=True
    )

    class Meta:
        model = PreemptionRequest
        fields = [
            "id",
            "beneficiary",
            "beneficiary_name",
            "target_node",
            "target_hostname",
            "pool",
            "status",
            "fail_reason",
            "victims",
            "created_at",
            "updated_at",
            "resolved_at",
        ]
        read_only_fields = fields


class SchedulingDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchedulingDecision
        fields = [
            "id",
            "kind",
            "pool",
            "job",
            "node",
            "preemption",
            "summary",
            "detail",
            "created_at",
        ]
        read_only_fields = fields
