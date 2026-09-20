"""
后台调度循环：周期性执行
1. 心跳失联检测；2. 运行超时结算；3. 所有资源池调度。

生产中可以多副本同时运行该进程——资源池咨询锁保证同池调度串行，
拿不到锁的副本会跳过本轮，不会重复分配或超卖。
"""
import time

from django.core.management.base import BaseCommand

from scheduler import engine, reconcile


class Command(BaseCommand):
    help = "运行调度后台循环（失联检测 / 超时结算 / 调度）"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=3.0,
                            help="循环间隔秒数（默认 3s）")
        parser.add_argument("--once", action="store_true",
                            help="只执行一轮后退出（测试/演示用）")

    def handle(self, *args, **options):
        interval = options["interval"]
        once = options["once"]
        self.stdout.write(self.style.SUCCESS(
            f"调度循环启动，间隔 {interval}s（once={once}）"
        ))
        while True:
            try:
                stale = reconcile.detect_stale_nodes()
                if stale:
                    self.stdout.write(self.style.WARNING(
                        f"失联节点下线: {stale}"
                    ))
                timed_runs = reconcile.timeout_expired_runs()
                if timed_runs:
                    self.stdout.write(self.style.WARNING(
                        f"运行超时作业: {timed_runs}"
                    ))
                results = engine.schedule_all_pools()
                for r in results:
                    if r.scheduled or r.preemptions or r.timed_out_queued:
                        self.stdout.write(
                            f"pool={r.pool_id} 分配={r.scheduled} "
                            f"抢占={r.preemptions} 排队超时={r.timed_out_queued}"
                        )
            except Exception as exc:  # 循环不能因偶发错误退出
                self.stderr.write(self.style.ERROR(f"调度循环异常: {exc}"))

            if once:
                self.stdout.write("单轮模式，退出。")
                break
            time.sleep(interval)
