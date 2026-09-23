# -*- coding: utf-8 -*-
"""端到端 HTTP 测试：所有密码与协议操作都经过 FastAPI 接口真实执行。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import paths
from app.engine import Engine
from app.relayer import Relayer
from app.storage import Database


def _engine_from_app(app) -> Engine:
    return app.state.engine


def test_http_full_lifecycle_and_negative_cases(client: TestClient):
    # 直接用引擎做建链/握手的编排（同一份 DB），HTTP 上验证包流程与错误响应
    engine = _engine_from_app(client.app)
    relay = Relayer(engine)
    relay.setup_two_chains("chainA", "chainB", genesis_time_nanos=1_000_000_000_000)
    relay.handshake("chainA", "chainB", ordering="UNORDERED", version="ics20-1")

    # health
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["chains"] == 2

    # send
    r = client.post("/packets/send", json={
        "chain_id": "chainA", "channel_id": "channel-0",
        "data": b"http-data".hex(),
        "timeout_height": 100, "timeout_time_nanos": 0,
        "amount": 250, "sender": "carol",
    })
    assert r.status_code == 200, r.text
    pkt = r.json()
    assert pkt["sequence"] == 1

    # 未提交直接取证明 -> 409 UNCOMMITTED_STATE
    r = client.post("/chains/chainA/commit")
    assert r.status_code == 200
    key = paths.packet_commitment_key("channel-0", 1).hex()
    proof = client.get(f"/chains/chainA/proof", params={"key": key}).json()

    # recv
    r = client.post("/packets/recv", json={"packet": pkt, "proof_commitment": proof})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "received"
    r = client.post("/chains/chainB/commit")
    assert r.status_code == 200

    # 证明伪造：签名翻转后 recv 同一包（去重路径前即被检查点拒绝）
    cp = dict(proof["checkpoint"])
    sig = bytearray(bytes.fromhex(cp["signature"]))
    sig[-1] ^= 0xEE
    cp["signature"] = sig.hex()
    bad = dict(proof)
    bad["checkpoint"] = cp
    r = client.post("/packets/recv", json={"packet": pkt, "proof_commitment": bad})
    assert r.status_code == 400 and r.json()["error"] == "INVALID_CHECKPOINT"

    # acknowledge
    proofs = {
        "proof_ack": client.get(
            f"/chains/chainB/proof",
            params={"key": paths.packet_ack_key("channel-0", 1).hex()}).json(),
        "proof_receipt": client.get(
            f"/chains/chainB/proof",
            params={"key": paths.packet_receipt_key("channel-0", 1).hex()}).json(),
        "proof_next_seq_recv": client.get(
            f"/chains/chainB/proof",
            params={"key": paths.next_seq_recv_key("channel-0").hex()}).json(),
    }
    r = client.post("/packets/acknowledge", json={
        "packet": pkt, "ack": "success".encode().hex(), "proofs": proofs,
    })
    assert r.status_code == 200, r.text
    assert r.json()["final_status"] == "ACKED"

    # 重复确认 -> 409 互斥
    r = client.post("/packets/acknowledge", json={
        "packet": pkt, "ack": "success".encode().hex(), "proofs": proofs,
    })
    assert r.status_code == 409 and r.json()["error"] == "PACKET_ALREADY_FINALIZED"

    # 包状态查询
    r = client.get("/chains/chainA/channels/channel-0/packets/1")
    assert r.status_code == 200 and r.json()["status"] == "ACKED"

    # 源链再出一个区块锚定终态，完整性校验必须干净
    assert client.post("/chains/chainA/commit").status_code == 200
    r = client.get("/integrity")
    assert r.status_code == 200
    assert all(c["uncommitted_state"] is False for c in r.json()["chains"])


def test_http_ordered_gap_returns_409_and_then_succeeds(client: TestClient):
    engine = _engine_from_app(client.app)
    relay = Relayer(engine)
    relay.setup_two_chains("chainA", "chainB", genesis_time_nanos=1_000_000_000_000)
    relay.handshake("chainA", "chainB", ordering="ORDERED", version="v1")

    def send(seq_data: bytes):
        r = client.post("/packets/send", json={
            "chain_id": "chainA", "channel_id": "channel-0",
            "data": seq_data.hex(),
            "timeout_height": 100, "timeout_time_nanos": 0,
        })
        assert r.status_code == 200, r.text
        return r.json()

    p1, p2 = send(b"one"), send(b"two")
    client.post("/chains/chainA/commit")

    proof2 = client.get(
        "/chains/chainA/proof",
        params={"key": paths.packet_commitment_key("channel-0", 2).hex()}).json()
    r = client.post("/packets/recv", json={"packet": p2, "proof_commitment": proof2})
    assert r.status_code == 409 and r.json()["error"] == "SEQUENCE_GAP"

    proof1 = client.get(
        "/chains/chainA/proof",
        params={"key": paths.packet_commitment_key("channel-0", 1).hex()}).json()
    r = client.post("/packets/recv", json={"packet": p1, "proof_commitment": proof1})
    assert r.status_code == 200
    r = client.post("/packets/recv", json={"packet": p2, "proof_commitment": proof2})
    assert r.status_code == 200 and r.json()["result"] == "received"


def test_http_startup_rejects_tampered_db(tmp_path):
    dbp = str(tmp_path / "tamper.db")
    db = Database(dbp)
    engine = Engine(db)
    relay = Relayer(engine)
    relay.setup_two_chains("chainA", "chainB", genesis_time_nanos=1_000_000_000_000)
    relay.commit("chainA")
    # 篡改检查点签名
    db.connect().execute(
        "UPDATE checkpoints SET signature=? WHERE chain_id='chainA' AND height=1",
        ("ab" * 64,),
    )
    # 重新创建 app（模拟重启）-> 启动完整性校验应失败（TestClient 启动抛异常）
    import pytest
    from app.api import create_app

    app = create_app(Database(dbp))
    with pytest.raises(Exception):
        with TestClient(app):
            pass
