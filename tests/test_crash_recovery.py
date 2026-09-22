"""处罚崩溃恢复测试。

场景一（真实进程崩溃）：以 SLASHER_CRASH_AFTER_EVIDENCE=1 启动 uvicorn 子进程，
证据提交后、惩罚应用前进程硬退出（exit 27）。重启后调用恢复接口补罚，
且重放相同投票不会二次处罚。

场景二：惩罚函数本身的幂等性（重复调用返回 already_applied，恢复为空操作）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import slashing
from tests.conftest import make_chain, make_validator, signed_vote_body


def _key(seed: int = 1):
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int, crash: bool) -> subprocess.Popen:
    env = dict(os.environ)
    if crash:
        env["SLASHER_CRASH_AFTER_EVIDENCE"] = "1"
    else:
        env.pop("SLASHER_CRASH_AFTER_EVIDENCE", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def _wait_ready(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0, trust_env=False)
            if r.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise RuntimeError("server did not become ready")


@pytest.mark.slow
def test_crash_between_evidence_and_penalty_then_recover(client, db):
    """证据已提交、惩罚未落盘时进程崩溃 -> 重启恢复后恰好处罚一次。"""
    make_chain(db, "test-chain-1", 10)
    priv = _key(1)
    make_validator(db, priv, "test-chain-1", "alice", 100_000_000)
    with db.connection() as conn:
        slashing.freeze_epoch(conn, "test-chain-1", 0)
        conn.commit()

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = _start_server(port, crash=True)
    try:
        _wait_ready(port)
        a = signed_vote_body(priv, round=7, block_hash=b"\xa1" * 32)
        b = signed_vote_body(priv, round=7, block_hash=b"\xb2" * 32)
        with httpx.Client(timeout=5, trust_env=False) as c:
            r1 = c.post(f"{base}/api/v1/chains/test-chain-1/votes", json=a)
            assert r1.status_code == 200 and r1.json()["result"] == "first"
            # 第二张票触发证据提交后进程崩溃，响应可能丢失
            try:
                c.post(f"{base}/api/v1/chains/test-chain-1/votes", json=b)
            except httpx.TransportError:
                pass
        proc.wait(timeout=10)
        assert proc.returncode == 27, f"expected crash exit 27, got {proc.returncode}"
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    # 崩溃后：证据 pending，惩罚不存在
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT evidence_id, status FROM evidences")
            rows = cur.fetchall()
            assert len(rows) == 1
            eid = rows[0]["evidence_id"].hex()
            assert rows[0]["status"] == "pending"
            cur.execute("SELECT count(*) AS n FROM penalties")
            assert cur.fetchone()["n"] == 0

    # 正常重启并恢复
    proc2 = _start_server(port, crash=False)
    try:
        _wait_ready(port)
        with httpx.Client(timeout=5, trust_env=False) as c:
            rec = c.post(f"{base}/api/v1/admin/recover").json()
            assert eid in rec["recovered"]

            detail = c.get(f"{base}/api/v1/evidences/{eid}").json()
            assert detail["evidence"]["status"] == "punished"
            assert detail["penalty"]["slashed_power"] == 1_000_000

            # 重放相同投票：duplicate / already_evidence，绝不二次处罚
            r = c.post(f"{base}/api/v1/chains/test-chain-1/votes", json=a)
            assert r.json()["result"] in ("duplicate", "already_evidence")
            r = c.post(f"{base}/api/v1/chains/test-chain-1/votes", json=b)
            assert r.json()["result"] in ("duplicate", "already_evidence")

            # 再次恢复：幂等，无新增
            rec2 = c.post(f"{base}/api/v1/admin/recover").json()
            assert rec2["recovered"] == []
    finally:
        proc2.terminate()
        proc2.wait(timeout=5)

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM penalties")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT count(*) AS n FROM evidences")
            assert cur.fetchone()["n"] == 1


def test_apply_penalty_is_idempotent(client, db):
    make_chain(db, "test-chain-1", 10)
    priv = _key(1)
    pub = make_validator(db, priv, "test-chain-1", "alice", 100_000_000)
    with db.connection() as conn:
        slashing.freeze_epoch(conn, "test-chain-1", 0)
        conn.commit()

    a = signed_vote_body(priv, round=7, block_hash=b"\xa1" * 32)
    b = signed_vote_body(priv, round=7, block_hash=b"\xb2" * 32)
    with db.connection() as conn:
        slashing.ingest_vote(
            conn, chain_id=a["chain_id"], pubkey=pub, round=7,
            block_hash=bytes.fromhex(a["block_hash"]),
            signature=bytes.fromhex(a["signature"]), raw=a)
        out = slashing.ingest_vote(
            conn, chain_id=b["chain_id"], pubkey=pub, round=7,
            block_hash=bytes.fromhex(b["block_hash"]),
            signature=bytes.fromhex(b["signature"]), raw=b)
        conn.commit()
    eid = bytes.fromhex(out["evidence_id"])

    with db.connection() as conn:
        first = slashing.apply_penalty(conn, evidence_id=eid)
        conn.commit()
    assert first["already_applied"] is False
    assert first["slashed_power"] == 1_000_000

    with db.connection() as conn:
        second = slashing.apply_penalty(conn, evidence_id=eid)
        conn.commit()
    assert second["already_applied"] is True

    with db.connection() as conn:
        rec = slashing.recover_pending(conn)
        conn.commit()
    assert rec["recovered"] == []

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM penalties")
            assert cur.fetchone()["n"] == 1
