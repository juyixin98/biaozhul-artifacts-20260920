"""
服务启动时执行一次恢复对账：Allocation 表是分配事实来源，本命令校正
节点 available/occupied 状态并输出汇总。配合 entrypoint 在 migrate
之后、runserver 之前运行。
"""
from django.core.management.base import BaseCommand

from scheduler.reconcile import recover_on_startup


class Command(BaseCommand):
    help = "从数据库恢复分配关系并做一致性对账"

    def handle(self, *args, **options):
        summary = recover_on_startup()
        self.stdout.write(self.style.SUCCESS(
            "启动恢复完成: " + ", ".join(f"{k}={v}" for k, v in summary.items())
        ))
