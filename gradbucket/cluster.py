"""真实多进程集群编排（本地教学用）。

- 启动一个 uvicorn 子进程承载 FastAPI 主节点；
- 启动若干工作者子进程（:mod:`gradbucket.worker_cli`），各自独立 Python
  进程，通过 HTTP 通信；
- 主节点 begin 一轮、等待桶到齐、调用 seal，并打印桶世代与归约依据；
- 工作者非零退出（如模拟中断返回码 3）被如实上报，不伪装成功。

仅供本地演示，不含任何生产级进程监管。
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx
import numpy as np

from .graph import LinearModel, ModelConfig


@dataclass
class WorkerSpec:
    worker_id: str
    rank: int
    shard_sizes: str = ""          # 空=均匀
    omit: str = ""
    fail_after_bucket: int = -1
    withhold_bucket: int = -1
    linger_seconds: float = 1.5


def _wait_health(client: httpx.Client, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError("主节点未在超时内就绪")


def run_cluster(
    *,
    n_workers: int = 3,
    n_samples: int = 12,
    shard_sizes: list[int] | None = None,
    bucket_capacity: int = 2,
    liveness_timeout: float = 3.0,
    port: int = 8765,
    workers: list[WorkerSpec] | None = None,
    log_path: str = "logs/cluster.log",
) -> dict:
    """启动集群、跑一轮、返回 seal 响应。进程日志全部落盘（含子进程 stderr）。"""
    base_url = f"http://127.0.0.1:{port}"
    sizes_str = ",".join(map(str, shard_sizes)) if shard_sizes else ""
    specs = workers or [
        WorkerSpec(f"w{i}", i, shard_sizes=sizes_str) for i in range(n_workers)
    ]

    server_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "gradbucket.server:app", "--host", "127.0.0.1",
            "--port", str(port), "--log-level", "warning",
        ],
        env={**os.environ,
             "GRADBUCKET_LIVENESS_TIMEOUT": str(liveness_timeout)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            _wait_health(client)
            model = LinearModel(ModelConfig())
            params = model.param_specs()
            weights = {p.name: p.zeros().tolist() for p in params}
            client.post(
                "/rounds/begin",
                json={
                    "round_index": 0,
                    "params": [
                        {"name": p.name, "shape": list(p.shape), "dtype": p.dtype}
                        for p in params
                    ],
                    "weights": weights,
                    "workers": [s.worker_id for s in specs],
                    "bucket_capacity": bucket_capacity,
                    "known_zero_params": ["spare"],
                },
            )

            procs = []
            # 每个工作者都需要*完整*分片大小列表来各自确定自己的分片。
            full_sizes = (
                ",".join(map(str, shard_sizes)) if shard_sizes else ""
            )
            for s in specs:
                cmd = [
                    sys.executable, "-m", "gradbucket.worker_cli",
                    "--base-url", base_url,
                    "--worker-id", s.worker_id,
                    "--rank", str(s.rank),
                    "--n-workers", str(len(specs)),
                    "--n-samples", str(n_samples),
                    "--seed", "445",
                ]
                if full_sizes:
                    cmd += ["--shard-sizes", full_sizes]
                if s.omit:
                    cmd += ["--omit", s.omit]
                if s.fail_after_bucket >= 0:
                    cmd += ["--fail-after-bucket", str(s.fail_after_bucket)]
                if s.withhold_bucket >= 0:
                    cmd += ["--withhold-bucket", str(s.withhold_bucket)]
                # 心跳续约时长：正常完成者不需要长；压下桶的工作者需要
                # 长于主节点轮询窗口以维持"存活但欠桶"的状态。
                cmd += ["--linger-seconds", str(s.linger_seconds)]
                procs.append((s, subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                )))

            # 先等所有工作者完成首次接触（首次提交即刷新心跳）。
            # 在此之前 seal 会因"无心跳且缺桶"立即判失联，属误判。
            contact_deadline = time.time() + 15.0
            while time.time() < contact_deadline:
                view = client.get("/rounds/current").json()
                ages = view.get("worker_last_heartbeat_age", {})
                if ages and all(
                    a is not None and math.isfinite(float(a))
                    for a in ages.values()
                ):
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("工作者未在超时内完成首次接触")

            # 与工作者并发轮询 seal：
            # - 桶齐 -> ACCEPTED，立即结束；
            # - 欠桶者心跳超时 -> REJECTED_WORKER_LOST，立即结束；
            # - 欠桶但仍存活 -> INDETERMINATE_PENDING，轮询到窗口截止，
            #   如实返回"无法判定"（而不是把存活者拖死后改判拒绝）。
            terminal = {"ACCEPTED", "REJECTED_WORKER_LOST",
                        "REJECTED_NO_EVIDENCE"}
            poll_deadline = time.time() + liveness_timeout + 1.0
            seal: dict | None = None
            while time.time() < poll_deadline:
                time.sleep(0.1)
                seal = client.post("/rounds/0/seal").json()
                if seal.get("code") in terminal:
                    break

            # 收集工作者：ACCEPTED 后给正常工作者一点自然退出时间；
            # 仍存活的（如压下桶者）终止并记录非零退出码。
            worker_outputs: dict[str, str] = {}
            worker_rc: dict[str, int] = {}
            for s, proc in procs:
                try:
                    out, _ = proc.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        out, _ = proc.communicate(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        out, _ = proc.communicate()
                worker_outputs[s.worker_id] = out or ""
                worker_rc[s.worker_id] = proc.returncode

            diag = client.get("/diagnostics").json()
            result = {
                "seal": seal,
                "diagnostics": diag,
                "worker_return_codes": worker_rc,
            }
            _write_log(log_path, base_url, worker_outputs, worker_rc, seal, diag)
            return result
    finally:
        server_proc.terminate()
        try:
            server_proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.communicate()


def _write_log(path, base_url, worker_outputs, worker_rc, seal, diag) -> None:
    import json
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [f"# gradbucket cluster run @ {base_url}",
             f"worker return codes: {worker_rc}", ""]
    for w, out in worker_outputs.items():
        lines.append(f"=== worker {w} (rc={worker_rc[w]}) ===")
        lines.append(out.rstrip())
    lines.append("=== seal verdict ===")
    lines.append(json.dumps(seal, ensure_ascii=False, indent=2, sort_keys=True))
    lines.append("=== diagnostic verdict counts ===")
    lines.append(json.dumps(diag["verdicts"], ensure_ascii=False, sort_keys=True))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
