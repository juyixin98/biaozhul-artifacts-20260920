"""
创建演示数据：一个资源池（GPU/并发配额）+ 若干异构模拟节点 + 若干排队作业。
节点注册时立即上报一次心跳。重复执行不会产生重名数据。
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from scheduler.clock import get_clock
from scheduler.models import Job, Node, ResourcePool
from scheduler.reconcile import heartbeat

# (名称, GPU 数, 单卡显存 MB, 初始状态)
DEMO_NODES = [
    ("node-a1", 8, 32768, Node.Status.AVAILABLE),
    ("node-a2", 4, 16384, Node.Status.AVAILABLE),
    ("node-b1", 2, 8192, Node.Status.AVAILABLE),
]

# (名称, min, max, 显存要求, 优先级)
DEMO_JOBS = [
    ("train-embedder", 2, 4, 16384, 5),
    ("train-ranker-lo", 1, 2, 8192, 3),
    ("train-ranker-hi", 6, 8, 16384, 9),
    ("eval-small", 1, 1, 8192, 4),
    ("train-tiny", 1, 1, 4096, 2),
]


class Command(BaseCommand):
    help = "创建演示用资源池、模拟节点与排队作业"

    @transaction.atomic
    def handle(self, *args, **options):
        pool, _ = ResourcePool.objects.get_or_create(
            name="pool-default",
            defaults={"max_gpus": 12, "max_running_jobs": 3},
        )

        for name, gpus, mem, status in DEMO_NODES:
            node, created = Node.objects.get_or_create(
                name=name,
                defaults={
                    "pool": pool, "gpu_count": gpus,
                    "gpu_memory_mb": mem, "status": status,
                },
            )
            if created:
                heartbeat(node.id, gpu_count=gpus, gpu_memory_mb=mem)
                self.stdout.write(f"注册模拟节点 {name}: {gpus} GPU / {mem}MB")

        for name, mn, mx, mem, prio in DEMO_JOBS:
            _, created = Job.objects.get_or_create(
                name=name,
                defaults={
                    "pool": pool, "min_gpus": mn, "max_gpus": mx,
                    "gpu_memory_mb": mem, "priority": prio,
                    "status": Job.Status.QUEUED, "queued_at": get_clock().now(),
                },
            )
            if created:
                self.stdout.write(
                    f"入队作业 {name}: {mn}-{mx} GPU, 显存 {mem}MB, 优先级 {prio}"
                )

        self.stdout.write(self.style.SUCCESS(
            "演示数据就绪：POST /api/pools/1/schedule/ 触发调度"
        ))
