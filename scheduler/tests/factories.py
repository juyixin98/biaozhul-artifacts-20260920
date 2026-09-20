"""测试辅助：时间工厂、资源构造。"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from scheduler.models import Job, Node, ResourcePool


def make_pool(name="p", *, max_gpus=0, max_running_jobs=0) -> ResourcePool:
    return ResourcePool.objects.create(
        name=name, max_gpus=max_gpus, max_running_jobs=max_running_jobs
    )


def make_node(pool, name, gpus, mem=10000, *, status=Node.Status.AVAILABLE,
              heartbeat_ago_seconds=0, clock=None) -> Node:
    now = (clock.now() if clock else timezone.now())
    node = Node.objects.create(
        pool=pool, name=name, gpu_count=gpus, gpu_memory_mb=mem,
        status=status,
    )
    if heartbeat_ago_seconds is not None:
        node.last_heartbeat_at = now - timedelta(seconds=heartbeat_ago_seconds)
        node.save(update_fields=["last_heartbeat_at"])
    return node


def make_job(pool, name="j", *, min_gpus=1, max_gpus=1, mem=0, priority=5,
             status=Job.Status.QUEUED, queued_at=None, clock=None,
             run_timeout_seconds=None) -> Job:
    now = clock.now() if clock else timezone.now()
    return Job.objects.create(
        pool=pool, name=name, min_gpus=min_gpus, max_gpus=max_gpus,
        gpu_memory_mb=mem, priority=priority, status=status,
        queued_at=queued_at or now, run_timeout_seconds=run_timeout_seconds,
    )


def used_gpus_of_pool(pool) -> int:
    from django.db.models import Sum

    from scheduler.models import Allocation

    return (
        Allocation.objects.filter(node__pool=pool)
        .aggregate(t=Sum("gpu_count"))["t"]
        or 0
    )


def node_used(node) -> int:
    from django.db.models import Sum

    from scheduler.models import Allocation

    return (
        Allocation.objects.filter(node=node).aggregate(t=Sum("gpu_count"))["t"]
        or 0
    )
