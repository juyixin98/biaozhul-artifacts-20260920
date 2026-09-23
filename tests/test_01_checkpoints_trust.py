"""信任根测试：检查点缺失/错误密钥/错误签名/陈旧高度/伪造证明一律拒绝。"""

from __future__ import annotations

import pytest

from ibc_teach import relayer
from ibc_teach.store import ErrProof

from .conftest import deliver, finalize, make_channel, send_packet


def test_missing_checkpoint_rejected_at_service_layer(client):
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    for field in ("chain_id", "height", "app_hash", "signature", "verify_key"):
        cp = dict(body["checkpoint"])
        cp.pop(field, None)
        with pytest.raises(ErrProof):
            client.store.recv_packet(body["packet"], cp, body["proof"])


def test_missing_signature_rejected_over_http(client):
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    del body["checkpoint"]["signature"]
    r = client.post("/packets/recv", json=body)
    assert r.status_code in (400, 422)  # 模式层或协议层，总之拒绝


def test_wrong_verify_key_rejected(client):
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    # 换成另一条链的共识密钥：信任根不匹配。
    body["checkpoint"]["verify_key"] = client.store.verify_key("chain-b")
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 400 and r.json()["error"] == "proof_verification_failed"


def test_tampered_signature_rejected(client):
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    body["checkpoint"]["signature"] = "00" * 64
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 400


def test_tampered_height_forges_old_signature_rejected(client):
    """把高度字段改掉（签名仍为原高度的真签名）必须验签失败。"""
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    body["checkpoint"]["height"] = 2
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 400


def test_stale_checkpoint_height_rollback_rejected(client):
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    h_a = 1
    body = relayer.recv_body(client.store, p, h_a)
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 200, r.text
    # 客户端已信任高度 1；重放创世高度 0 的旧检查点 -> 拒绝。
    b0 = client.store.block("chain-a", 0)
    stale_cp = {
        "chain_id": "chain-a",
        "height": 0,
        "time_ns": b0["time_ns"],
        "app_hash": b0["app_hash"],
        "previous_app_hash": b0["previous_app_hash"],
        "signature": b0["signature_hex"],
        "verify_key": client.store.verify_key("chain-a"),
    }
    r2 = client.post("/packets/recv", json={
        "packet": body["packet"],
        "checkpoint": stale_cp,
        "proof": body["proof"],  # 高度 1 的证明，与旧检查点根不匹配
    })
    assert r2.status_code == 400 and "stale" in r2.json()["message"]


def test_tampered_proof_step_rejected(client):
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    step = body["proof"]["steps"][10]
    s = step["sibling"]
    step["sibling"] = ("00" if s[:2] != "00" else "11") + s[2:]
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 400


def test_proof_against_wrong_root_rejected(client):
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    body["checkpoint"]["app_hash"] = "00" * 32  # 根被换，签名随之失效
    body["checkpoint"]["previous_app_hash"] = body["checkpoint"]["app_hash"]
    body["checkpoint"]["signature"] = "00" * 64
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 400


def test_non_membership_proof_for_commitment_rejected(client):
    """未发包（源链无承诺）时，非成员证明不能冒充成员证明完成接收。"""
    make_channel(client, "unordered")
    # 先在目的链造一个高度，源链不出包含承诺的块。
    finalize(client, "chain-a")
    p = send_packet(client)  # 未 finalize，高度 1 快照里没有该承诺
    from ibc_teach import store as st
    key = st.commitment_key(p["src_port"], p["src_channel"], p["sequence"])
    pr = client.store.proof("chain-a", 1, key)
    assert pr["exists"] is False
    body = {
        "packet": relayer.packet_msg(p),
        "checkpoint": pr["checkpoint"],
        "proof": {"steps": pr["proof"]["steps"]},
    }
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 400
