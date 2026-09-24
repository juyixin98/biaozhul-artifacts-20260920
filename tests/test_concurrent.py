"""真实 HTTP 服务器（独立子进程 uvicorn）并发验收。

TestClient 是同进程内串行的，无法体现"并发请求"；这里在一个独立子进程里启动真实的
uvicorn 服务（独立临时数据库，进程隔离，无需 monkey-patch），主进程用 httpx 多线程
同时打请求，验收：

1. 数据库不会落入任何顶点/边冲突（乐观锁 + 提交前重新校验 + 主键兜底）；
2. 竞争同一时空资源的并发请求中，输家得到 412/409，赢家可以拿新版本重试成功。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server():
    tmp = tempfile.mkdtemp(prefix="spatio_concurrent_")
    db_file = os.path.join(tmp, "server.db")
    port = _free_port()
    env = dict(os.environ, SPATIO_DB=db_file, PYTHONPATH=ROOT)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=base, timeout=10, trust_env=False)
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"uvicorn 提前退出：\n{out}")
        try:
            if client.get("/health").status_code == 200:
                ready = True
                break
        except httpx.TransportError:
            time.sleep(0.15)
    if not ready:
        proc.terminate()
        raise RuntimeError("测试服务器未在 20s 内就绪")

    client.post("/admin/reset")
    yield client
    client.close()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _simulate(reservations):
    paths = {r["robot_id"]: [tuple(c) for c in r["path"]] for r in reservations}
    max_len = max((len(p) for p in paths.values()), default=0)
    vertex, edge = [], []
    for t in range(max_len):
        cells, moves = {}, {}
        for rid, p in paths.items():
            cell = p[t] if t < len(p) else p[-1]
            cells.setdefault(cell, []).append(rid)
            if t < len(p) - 1:
                moves[rid] = (p[t], p[t + 1])
        for cell, rids in cells.items():
            if len(rids) > 1:
                vertex.append((t, cell, rids))
        rids = list(moves)
        for i in range(len(rids)):
            for j in range(i + 1, len(rids)):
                a, b = rids[i], rids[j]
                if (
                    moves[a][0] == moves[b][1]
                    and moves[a][1] == moves[b][0]
                    and moves[a][0] != moves[a][1]
                ):
                    edge.append((t, a, b))
    return vertex, edge


def test_eight_concurrent_requests_never_corrupt_table(server):
    """8 个并发请求（R1..R8 各一个）冲向同一个目标格。最多 1 个赢家；
    其余都被协议显式拒绝（无 5xx），且库里绝无顶点/边冲突。"""
    client = server
    client.put("/map", json={"width": 20, "height": 1, "obstacles": []})
    for i in range(8):
        client.put(f"/robots/R{i + 1}", json={"start": {"x": i, "y": 0}})

    lock_results = []

    def send(robot):
        r = client.post(
            "/reservations/plan",
            json={"robot_id": robot, "goal": {"x": 19, "y": 0}},
        )
        return r.status_code, r.json()

    def worker(robot):
        lock_results.append(send(robot))

    threads = [threading.Thread(target=worker, args=(f"R{i+1}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = [s for s, _ in lock_results]
    assert all(s < 500 for s in statuses), lock_results
    successes = [b for s, b in lock_results if s == 200]
    assert len(successes) == 1, f"同一永久终点只能有一个赢家：{successes}"
    codes = sorted(b.get("error", {}).get("code") for s, b in lock_results if s != 200)
    assert codes, "应存在被拒绝的请求"

    state = client.get("/state").json()
    vertex, edge = _simulate(state["reservations"])
    assert not vertex and not edge, (vertex, edge)


def test_loser_can_retry_with_fresh_version_and_succeed(server):
    """两个并发但实际不冲突的请求都带 expected_reservation_version=0：先提交者赢，
    后提交者拿到 412 版本冲突，随后用新版本重试成功。"""
    client = server
    client.post("/admin/reset")
    client.put("/map", json={"width": 5, "height": 1, "obstacles": []})
    client.put("/robots/R1", json={"start": {"x": 0, "y": 0}})
    client.put("/robots/R2", json={"start": {"x": 4, "y": 0}})

    # R1 走到 (3,0)，R2 走到 (4,0)（自己起点，纯等待路径）——互不冲突。
    out = {}

    def send(robot, goal):
        r = client.post(
            "/reservations/plan",
            json={
                "robot_id": robot,
                "goal": {"x": goal, "y": 0},
                "expected_reservation_version": 0,
            },
        )
        out[robot] = (r.status_code, r.json())

    t1 = threading.Thread(target=send, args=("R1", 3))
    t2 = threading.Thread(target=send, args=("R2", 4))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(s for s, _ in out.values())
    assert statuses == [200, 412], out
    loser = [r for r, (s, _) in out.items() if s == 412][0]
    assert out[loser][1]["error"]["code"] in {
        "RESV_STALE",
        "RESV_CHANGED_DURING_PLANNING",
    }

    fresh = client.get("/state").json()["reservation_version"]
    retry = client.post(
        "/reservations/plan",
        json={
            "robot_id": loser,
            "goal": {"x": 3 if loser == "R1" else 4, "y": 0},
            "expected_reservation_version": fresh,
        },
    )
    assert retry.status_code == 200, retry.text

    state = client.get("/state").json()
    vertex, edge = _simulate(state["reservations"])
    assert not vertex and not edge
