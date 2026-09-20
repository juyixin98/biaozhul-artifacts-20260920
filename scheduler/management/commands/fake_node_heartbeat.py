"""
模拟节点守护进程：对指定节点周期性上报心跳（并可附带 GPU 数量/显存），
用来演示“失联检测 / 恢复”：Ctrl-C 停掉本进程即模拟节点失联，
再启动即模拟恢复。
"""
import time

from django.core.management.base import BaseCommand

from scheduler.models import Node
from scheduler.reconcile import heartbeat


class Command(BaseCommand):
    help = "模拟某节点周期性上报心跳"

    def add_arguments(self, parser):
        parser.add_argument("node", help="节点名称")
        parser.add_argument("--interval", type=float, default=5.0,
                            help="心跳间隔秒数（默认 5s）")
        parser.add_argument("--gpu-count", type=int, required=False)
        parser.add_argument("--gpu-memory-mb", type=int, required=False)

    def handle(self, *args, **options):
        node = Node.objects.get(name=options["node"])
        interval = options["interval"]
        self.stdout.write(self.style.SUCCESS(
            f"模拟节点 {node.name} 开始心跳（间隔 {interval}s）"
        ))
        while True:
            heartbeat(
                node.id,
                gpu_count=options["gpu_count"],
                gpu_memory_mb=options["gpu_memory_mb"],
            )
            self.stdout.write(f"heartbeat -> {node.name}")
            time.sleep(interval)
