import signal

from django.core.management.base import BaseCommand

from engine.worker import Worker


class Command(BaseCommand):
    help = "启动一个分析任务工作进程（数据库队列消费者）。"

    def add_arguments(self, parser):
        parser.add_argument("--worker-id", default="", help="固定 worker id（默认随机）")
        parser.add_argument("--lease-seconds", type=int, default=None)
        parser.add_argument("--poll-interval", type=float, default=None)
        parser.add_argument("--once", action="store_true", help="处理至多一个任务后退出（测试用）")
        parser.add_argument("--max-tasks", type=int, default=0, help="处理 N 个任务后退出（0=不限）")

    def handle(self, *args, **opts):
        worker = Worker(
            worker_id=opts["worker_id"] or None,
            lease_seconds=opts["lease_seconds"],
            poll_interval=opts["poll_interval"],
            once=opts["once"],
            max_tasks=opts["max_tasks"],
        )
        signal.signal(signal.SIGTERM, worker.shutdown)
        signal.signal(signal.SIGINT, worker.shutdown)
        self.stdout.write(f"worker {worker.worker_id} started")
        worker.run()
        self.stdout.write(
            f"worker {worker.worker_id} stopped, tasks_done={worker._tasks_done}"
        )
