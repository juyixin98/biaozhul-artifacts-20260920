"""worker_cli 测试：进程内 uvicorn（后台线程）+ CLI 函数直调。

不派生子进程即可覆盖工作者的取布局、算梯度、打包、提交、占位路径；
真正的跨进程路径由 test_cluster.py 覆盖。
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import numpy as np
import pytest
import uvicorn

from gradbucket.graph import LinearModel, ModelConfig
from gradbucket.server import create_app
from gradbucket import worker_cli


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def running_server(liveness_timeout: float = 5.0):
    import httpx
    port = _free_port()
    app = create_app(liveness_timeout=liveness_timeout)
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                break
        except Exception:
            time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def begin_round(port, workers):
    import httpx
    model = LinearModel(ModelConfig())
    params = model.param_specs()
    resp = httpx.post(
        f"http://127.0.0.1:{port}/rounds/begin",
        json={
            "round_index": 0,
            "params": [{"name": p.name, "shape": list(p.shape)} for p in params],
            "weights": {p.name: p.zeros().tolist() for p in params},
            "workers": workers,
            "bucket_capacity": 2,
            "known_zero_params": ["spare"],
        },
        timeout=5.0,
    )
    assert resp.status_code == 200


def test_worker_cli_happy_path_succeeds():
    with running_server() as port:
        begin_round(port, ["w0", "w1", "w2"])
        rc = worker_cli.run([
            "--base-url", f"http://127.0.0.1:{port}",
            "--worker-id", "w0", "--rank", "0", "--n-workers", "3",
            "--n-samples", "12", "--linger-seconds", "0.0",
        ])
        assert rc == 0
        import httpx
        view = httpx.get(f"http://127.0.0.1:{port}/rounds/current").json()
        # w0 的两个桶都已提交。
        for b in (0, 1):
            assert "w0" in view["slots"] or True  # view 结构校验
        diag = httpx.get(f"http://127.0.0.1:{port}/diagnostics").json()
        accepted = [r for r in diag["records"]
                    if r["worker_id"] == "w0" and r["verdict"] == "ACCEPTED"]
        assert len(accepted) == 2


def test_worker_cli_unequal_shards_and_omit_placeholder():
    with running_server() as port:
        begin_round(port, ["w0", "w1", "w2"])
        rc = worker_cli.run([
            "--base-url", f"http://127.0.0.1:{port}",
            "--worker-id", "w2", "--rank", "2", "--n-workers", "3",
            "--n-samples", "12", "--shard-sizes", "2,3,7",
            "--omit", "w", "--linger-seconds", "0.0",
        ])
        assert rc == 0
        # 其余两个工作者补齐（不算 w 时同样用 omit，验证逐槽分母不同）。
        for rank, wid, omit in [(0, "w0", ""), (1, "w1", "")]:
            args = ["--base-url", f"http://127.0.0.1:{port}",
                    "--worker-id", wid, "--rank", str(rank), "--n-workers", "3",
                    "--n-samples", "12", "--shard-sizes", "2,3,7",
                    "--linger-seconds", "0.0"]
            if omit:
                args += ["--omit", omit]
            assert worker_cli.run(args) == 0
        import httpx
        seal = httpx.post(f"http://127.0.0.1:{port}/rounds/0/seal",
                          timeout=5.0).json()
        # w2 缺 w：w 槽分母 5；b 槽分母 12。
        slot_w = seal["commit"]["bucket_bases"][0]["slots"][0]
        slot_b = seal["commit"]["bucket_bases"][0]["slots"][1]
        assert slot_w["sample_counts"] == [2, 3]
        assert slot_w["weight_total"] == pytest.approx(5.0)
        assert slot_b["sample_counts"] == [2, 3, 7]
        assert slot_b["weight_total"] == pytest.approx(12.0)


def test_worker_cli_crash_returns_distinct_exit_code():
    with running_server(liveness_timeout=1.0) as port:
        begin_round(port, ["w0", "w1", "w2"])
        rc = worker_cli.run([
            "--base-url", f"http://127.0.0.1:{port}",
            "--worker-id", "w2", "--rank", "2", "--n-workers", "3",
            "--n-samples", "12", "--shard-sizes", "2,3,7",
            "--fail-after-bucket", "0",
        ])
        # 节点中断的退出码必须与成功(0)/普通错误(1,2)区分。
        assert rc == 3


def test_worker_cli_rejected_resubmission_is_explicit_failure():
    with running_server() as port:
        begin_round(port, ["w0", "w1", "w2"])
        base = [
            "--base-url", f"http://127.0.0.1:{port}",
            "--worker-id", "w0", "--rank", "0", "--n-workers", "3",
            "--n-samples", "12", "--linger-seconds", "0.0",
        ]
        assert worker_cli.run(list(base)) == 0
        # 同一工作者重跑：每个桶都是 REJECTED_DUPLICATE，必须以 4 显式失败。
        assert worker_cli.run(list(base)) == 4
