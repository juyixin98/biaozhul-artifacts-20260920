"""Simulated GPU node agent.

This command pretends to be a physical machine:

  1. POST /api/nodes/register/  with its GPU count and per-card memory;
  2. POST /api/nodes/heartbeat/ every --interval seconds;
  3. polls /api/allocations/?node=<hostname> to discover jobs placed on it;
  4. after --job-lifetime seconds it POSTs /api/jobs/<id>/complete/
     (simulating a finished training run) and frees the GPUs;
  5. when one of its jobs flips to PREEMPTING it waits --preempt-delay
     seconds and POSTs /api/jobs/<id>/release/ (simulated clean GPU release).

It uses only the public REST API and the standard library, so it also works
against the Dockerized web service from outside the cluster.
"""
import json
import logging
import signal
import time
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def _request(base, method, path, payload=None):
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"detail": body}


class Command(BaseCommand):
    help = "Run a simulated GPU node that heartbeats and finishes jobs."

    def add_arguments(self, parser):
        parser.add_argument("--api-url", default="http://127.0.0.1:8000")
        parser.add_argument("--pool", default="demo")
        parser.add_argument("--hostname", required=True)
        parser.add_argument("--gpus", type=int, default=8)
        parser.add_argument("--gpu-memory", type=int, default=81920)
        parser.add_argument("--interval", type=float, default=2.0)
        parser.add_argument(
            "--job-lifetime",
            type=float,
            default=60.0,
            help="Simulated seconds a job runs before completing.",
        )
        parser.add_argument(
            "--preempt-delay",
            type=float,
            default=2.0,
            help="Simulated seconds to release a preempted job's GPUs.",
        )

    def handle(self, *args, **opts):
        base = opts["api_url"]
        hostname = opts["hostname"]
        running = {"flag": True}

        def _stop(signum, frame):
            running["flag"] = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        # 1. Register (idempotent).
        status, body = _request(
            base,
            "POST",
            "/api/nodes/register/",
            {
                "pool": opts["pool"],
                "hostname": hostname,
                "gpu_count": opts["gpus"],
                "gpu_memory_mb": opts["gpu_memory"],
            },
        )
        if status not in (200, 201):
            self.stderr.write(f"register failed: {status} {body}")
            return
        self.stdout.write(self.style.SUCCESS(f"[{hostname}] registered: {body}"))

        # job_id -> {"started": monotonic, "mode": "run"|"preempt"}
        local = {}
        next_hb = 0.0

        while running["flag"]:
            now = time.monotonic()

            # 2. Heartbeat.
            if now >= next_hb:
                code, _ = _request(
                    base,
                    "POST",
                    "/api/nodes/heartbeat/",
                    {"hostname": hostname},
                )
                if code != 200:
                    self.stderr.write(f"[{hostname}] heartbeat -> {code}")
                next_hb = now + opts["interval"]

            # 3. What is allocated to this node right now?
            code, allocations = _request(
                base, "GET", f"/api/allocations/?node={hostname}"
            )
            current_jobs = {}
            if code == 200:
                for alloc in allocations:
                    current_jobs[alloc["job"]] = alloc

            for jid in list(local.keys()):
                if jid not in current_jobs:
                    # Allocation vanished (we finished it / it was cancelled).
                    local.pop(jid, None)

            for jid, alloc in current_jobs.items():
                code, job = _request(base, "GET", f"/api/jobs/{jid}/")
                if code != 200:
                    continue
                state = job["state"]

                if state == "RUNNING":
                    info = local.setdefault(jid, {"started": now})
                    if now - info["started"] >= opts["job_lifetime"]:
                        c, b = _request(
                            base, "POST", f"/api/jobs/{jid}/complete/", {}
                        )
                        self.stdout.write(
                            f"[{hostname}] job {jid} complete -> {c} {b.get('state')}"
                        )
                        local.pop(jid, None)

                elif state == "PREEMPTING":
                    info = local.setdefault(jid, {"started": now})
                    if not info.get("preempt_since"):
                        info["preempt_since"] = now
                        self.stdout.write(
                            f"[{hostname}] job {jid} marked PREEMPTING; releasing GPUs"
                        )
                    if now - info["preempt_since"] >= opts["preempt_delay"]:
                        c, b = _request(
                            base, "POST", f"/api/jobs/{jid}/release/", {}
                        )
                        self.stdout.write(
                            f"[{hostname}] job {jid} released after preemption -> {c}"
                        )
                        local.pop(jid, None)

                else:
                    local.pop(jid, None)

            time.sleep(opts["interval"])

        self.stdout.write(f"[{hostname}] mock node stopped")
