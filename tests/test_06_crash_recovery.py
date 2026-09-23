"""崩溃恢复：同库重开恢复状态、篡改检测、真实进程 kill -9 后重启。"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ibc_teach import relayer

from .conftest import deliver, finalize, make_channel, send_packet


def test_reopen_store_recovers_state(db_path):
    from ibc_teach.store import Store

    s = Store(db_path)
    s.create_channel(ordering="ordered", version="v1")
    p = s.send_packet(
        src_chain="chain-a", src_port="port-a", src_channel="channel-0",
        timeout_height=5, timeout_time_ns=0, data_hex="aa", amount=7,
    )
    s.finalize_block("chain-a")
    del s

    # 重新打开：状态完整保留，完整性校验通过。
    s2 = Store(db_path)
    report = s2.integrity_check()
    assert report["ok"], report["problems"]
    ch = s2.get_channel("chain-a", "port-a", "channel-0")
    assert ch.ordering == "ordered" and ch.next_seq_send == 2
    pkt = s2.get_packet("chain-a", "port-a", "channel-0", p["sequence"])
    assert pkt.status == "SENT" and pkt.amount == 7
    # 可以继续推进生命周期。
    s2.finalize_block("chain-b")
    body = relayer.recv_body(s2, pkt.__dict__, 1)
    r = s2.recv_packet(body["packet"], body["checkpoint"], body["proof"])
    assert r["result"] == "delivered"


def test_tampered_snapshot_detected(db_path):
    from ibc_teach.store import ErrIntegrity, Store

    s = Store(db_path)
    s.create_channel(ordering="unordered", version="v1")
    s.finalize_block("chain-a")
    del s

    import sqlite3

    # 把块 1 快照里某个十六进制值改写（长度不变），重算出的状态根必然不同。
    conn = sqlite3.connect(db_path)
    snap = conn.execute(
        "SELECT kv_json FROM kv_snapshots WHERE chain_id='chain-a' AND height=1"
    ).fetchone()[0]
    data = json.loads(snap)
    assert data, "expected a non-empty committed snapshot"
    k = next(iter(data))
    v = data[k]
    flipped = ("11" if v[:2] != "11" else "22") + v[2:]
    data[k] = flipped
    conn.execute(
        "UPDATE kv_snapshots SET kv_json=? WHERE chain_id='chain-a' AND height=1",
        (json.dumps(data, sort_keys=True),),
    )
    conn.commit()
    conn.close()

    s2 = Store(db_path)
    report = s2.integrity_check()
    assert report["ok"] is False
    assert any("app_hash mismatch" in p or "snapshot" in p for p in report["problems"])
    with pytest.raises(RuntimeError):
        # FastAPI 启动钩子遇到脏库应拒绝启动。
        from fastapi.testclient import TestClient

        from ibc_teach.app import create_app

        with TestClient(create_app(db_path)):
            pass
    del s2
    _ = ErrIntegrity


def test_tampered_block_signature_detected(db_path):
    from ibc_teach.store import Store

    s = Store(db_path)
    s.create_channel(ordering="unordered", version="v1")
    s.finalize_block("chain-a")
    del s

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE blocks SET signature_hex=? WHERE chain_id='chain-a' AND height=1",
        ("00" * 64,),
    )
    conn.commit()
    conn.close()

    report = Store(db_path).integrity_check()
    assert report["ok"] is False
    assert any("signature invalid" in p for p in report["problems"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(url: str, timeout_s: float = 10.0) -> dict:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                return json.loads(r.read())
        except Exception as e:  # 进程尚未起好
            last = e
            time.sleep(0.1)
    raise RuntimeError(f"server did not become healthy: {last}")


def _post(url: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_real_process_kill_and_restart(tmp_path):
    """真实 uvicorn 进程：建通道、发包、出块后 kill -9，重启状态可续。"""
    db = tmp_path / "proc.db"
    port = _free_port()
    env = dict(os.environ, IBC_TEACH_DB=str(db), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    cmd = [sys.executable, "-m", "uvicorn", "ibc_teach.app:app",
           "--host", "127.0.0.1", "--port", str(port)]

    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = f"http://127.0.0.1:{port}"
        _wait_health(url)
        assert _post(url, "/channels", {"ordering": "ordered", "version": "v1"})[0] == 201
        code, p = _post(url, "/packets/send", {
            "src_chain": "chain-a", "src_port": "port-a", "src_channel": "channel-0",
            "timeout_height": 0, "timeout_time_ns": 0,
            "data_hex": "cafe", "amount": 42,
        })
        assert code == 201, p
        assert _post(url, "/chains/chain-a/blocks")[0] == 200

        # 硬杀（无优雅退出），模拟崩溃。
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()

    # WAL 文件可能仍在；重启后应自动 checkpoint 恢复。
    assert db.exists()
    proc2 = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = f"http://127.0.0.1:{port}"
        _wait_health(url)  # 启动完整性校验必须通过，否则起不来
        with urllib.request.urlopen(url + "/admin/integrity", timeout=5) as r:
            report = json.loads(r.read())
        assert report["ok"], report
        with urllib.request.urlopen(url + "/packets", timeout=5) as r:
            packets = json.loads(r.read())
        assert len(packets) == 1 and packets[0]["status"] == "SENT"
        assert packets[0]["amount"] == 42
        chs = json.loads(urllib.request.urlopen(
            url + "/chains/chain-a/channels", timeout=5).read())
        assert chs[0]["ordering"] == "ordered" and chs[0]["next_seq_send"] == 2
    finally:
        proc2.send_signal(signal.SIGTERM)
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()
        err = proc2.stderr.read().decode()[:2000] if proc2.stderr else ""
        if proc2.returncode not in (0, -15):
            pytest.fail(f"server exited {proc2.returncode}: {err}")
