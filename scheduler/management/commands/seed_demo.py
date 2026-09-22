"""Seed a small demo: one resource pool, two nodes, a spread of jobs.

Run after the web service is up (or directly via manage.py). Useful with the
mock_node + run_scheduler processes to watch scheduling/preemption live.
"""
from django.core.management.base import BaseCommand

from scheduler import services


class Command(BaseCommand):
    help = "Create a demo resource pool, nodes and a set of jobs."

    def add_arguments(self, parser):
        parser.add_argument("--pool", default="demo")

    def handle(self, *args, **opts):
        from scheduler.models import ResourcePool

        name = opts["pool"]
        pool = ResourcePool.objects.filter(name=name).first()
        if pool is None:
            pool = services.create_pool(
                name=name,
                max_total_gpus=12,
                max_concurrent_jobs=6,
                description="demo pool (12 GPU quota, 6 concurrent jobs)",
            )
            self.stdout.write(self.style.SUCCESS(f"created pool {name}"))
        else:
            self.stdout.write(f"pool {name} already exists")

        # Note: nodes are normally created by the mock agent's register call;
        # we register them here (and send a heartbeat) so the seed is usable
        # standalone without agents.
        from django.utils import timezone

        for hostname, gpus, mem in (("node-a", 8, 81920), ("node-b", 4, 40960)):
            try:
                services.register_node(pool, hostname, gpus, mem)
                services.heartbeat(hostname, timezone.now())
                self.stdout.write(self.style.SUCCESS(f"registered {hostname}"))
            except services.SchedulerError as exc:
                self.stdout.write(f"{hostname}: {exc}")

        # Phase 1: saturate the nodes with low-priority filler jobs and run a
        # scheduling round so they occupy every GPU.
        filler_specs = [
            ("batch-train-1", 2, 4, 40960, 2),
            ("batch-train-2", 2, 4, 40960, 2),
            ("batch-train-3", 4, 8, 40960, 1),
            ("batch-train-4", 1, 2, 40960, 3),
        ]
        for job_name, mn, mx, mem, prio in filler_specs:
            job = services.create_job(pool, job_name, mn, mx, mem, prio)
            self.stdout.write(
                self.style.SUCCESS(
                    f"submitted {job_name} (job #{job.id}, p={prio})"
                )
            )

        from scheduler.clock import Clock
        from scheduler.engine import tick as engine_tick

        stats = engine_tick(Clock())
        self.stdout.write(
            self.style.SUCCESS(
                f"phase-1 tick: {stats['placements']} filler job(s) placed"
            )
        )

        # Phase 2: now submit the urgent (p=9) job. With the nodes full it
        # cannot fit and will trigger preemption against the p<=3 fillers on
        # the next scheduler tick; a mid-priority p=5 job simply waits.
        for job_name, mn, mx, mem, prio in (
            ("URGENT-finetune", 8, 8, 40960, 9),
            ("regular-eval", 2, 2, 20480, 5),
        ):
            job = services.create_job(pool, job_name, mn, mx, mem, prio)
            self.stdout.write(
                self.style.SUCCESS(
                    f"submitted {job_name} (job #{job.id}, p={prio})"
                )
            )
        self.stdout.write(
            "On the next scheduler tick URGENT-finetune (p=9) will preempt "
            "the p<=3 filler jobs. With mock nodes running, the workers "
            "release their GPUs and the urgent job is then placed."
        )
