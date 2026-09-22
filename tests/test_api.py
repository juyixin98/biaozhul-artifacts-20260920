"""HTTP 层测试：验签拒绝、跨链混用、重复票、双签归并、逆序到达一致。"""
from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.crypto import derive_pubkey, sign_vote
from tests.conftest import freeze, make_chain, make_validator, signed_vote_body

V1_PATH = "/api/v1/chains/test-chain-1/votes"


def _key(seed: int = 1):
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def _setup(db, power=100_000_000, chain="test-chain-1", epoch_length=10):
    make_chain(db, chain, epoch_length)
    priv = _key(1)
    pub = make_validator(db, priv, chain, "alice", power)
    freeze(db, chain, 0)
    return priv, pub


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["judge_version"].startswith("equivocation-rules/")


def test_valid_single_vote(client, db):
    priv, _ = _setup(db)
    r = client.post(V1_PATH, json=signed_vote_body(priv, round=3))
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "first"


def test_bad_signature_is_rejected(client, db):
    priv, pub = _setup(db)
    body = signed_vote_body(priv, round=3)
    sig = bytearray(bytes.fromhex(body["signature"]))
    sig[7] ^= 0xFF
    body["signature"] = bytes(sig).hex()
    r = client.post(V1_PATH, json=body)
    assert r.status_code == 422
    assert r.json()["error"] == "bad_signature"


def test_signature_from_other_validator_rejected(client, db):
    _setup(db)
    attacker = _key(9)
    victim_pub = derive_pubkey(_key(1))
    body = signed_vote_body(attacker, round=3)
    # 用受害者公钥替换载荷，签名对不上
    body["validator_pubkey"] = victim_pub.hex()
    r = client.post(V1_PATH, json=body)
    assert r.status_code == 422
    assert r.json()["error"] == "bad_signature"


def test_chain_id_mismatch_url_vs_body_rejected(client, db):
    priv, _ = _setup(db)
    body = signed_vote_body(priv, round=3)
    r = client.post("/api/v1/chains/other-chain/votes", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "chain_mismatch"


def test_vote_on_unregistered_chain_rejected(client, db):
    priv = _key(1)
    # 链都不存在
    body = signed_vote_body(priv, chain_id="ghost-chain", round=1)
    r = client.post("/api/v1/chains/ghost-chain/votes", json=body)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_validator_registered_only_on_one_chain_cannot_cross_use(client, db):
    """公钥只在 chain-A 注册；投给 chain-B 的票即使签名合法也必须拒绝。"""
    priv, pub = _setup(db, chain="chain-A")
    make_chain(db, "chain-B", 10)  # chain-B 存在，但 alice 未在 B 注册
    body = signed_vote_body(priv, chain_id="chain-B", round=2)
    r = client.post("/api/v1/chains/chain-B/votes", json=body)
    assert r.status_code == 422
    assert r.json()["error"] == "validator_not_registered"


def test_two_identical_votes_are_not_equivocation(client, db):
    priv, _ = _setup(db)
    body = signed_vote_body(priv, round=7, block_hash=b"\xa1" * 32)
    assert client.post(V1_PATH, json=body).json()["result"] == "first"
    r2 = client.post(V1_PATH, json=body)
    assert r2.json()["result"] == "duplicate"
    # 没有产生证据
    r = client.get("/api/v1/evidences")
    assert r.json()["count"] == 0


def test_double_sign_creates_one_evidence_and_one_penalty(client, db):
    priv, _ = _setup(db, power=100_000_000)
    a = signed_vote_body(priv, round=7, block_hash=b"\xa1" * 32)
    b = signed_vote_body(priv, round=7, block_hash=b"\xb2" * 32)
    client.post(V1_PATH, json=a)
    out = client.post(V1_PATH, json=b).json()
    assert out["result"] == "new_evidence"
    assert out["penalty"]["slashed_power"] == 1_000_000  # 100 * 1%
    assert out["penalty"]["base_power"] == 100_000_000
    assert out["penalty"]["snapshot_epoch"] == 0

    eid = out["evidence_id"]
    detail = client.get(f"/api/v1/evidences/{eid}").json()
    assert detail["evidence"]["status"] == "punished"
    assert detail["evidence"]["judge_version"]
    assert len(detail["evidence"]["raw_evidence"]) == 2
    assert detail["penalty"]["judge_version"] == detail["evidence"]["judge_version"]

    # 只有一份证据、一条惩罚
    assert client.get("/api/v1/evidences").json()["count"] == 1


def test_third_and_late_votes_do_not_punish_twice(client, db):
    priv, _ = _setup(db)
    a = signed_vote_body(priv, round=7, block_hash=b"\xa1" * 32)
    b = signed_vote_body(priv, round=7, block_hash=b"\xb2" * 32)
    c = signed_vote_body(priv, round=7, block_hash=b"\xc3" * 32)
    client.post(V1_PATH, json=a)
    eid = client.post(V1_PATH, json=b).json()["evidence_id"]
    # 迟到的第三张冲突票
    late = client.post(V1_PATH, json=c).json()
    assert late["result"] == "already_evidence"
    assert late["evidence_id"] == eid
    # 重复重放 A
    assert client.post(V1_PATH, json=a).json()["result"] == "duplicate"
    # 恢复也不会二次处罚
    rec = client.post("/api/v1/admin/recover").json()
    assert eid not in rec["recovered"]
    assert client.get("/api/v1/evidences").json()["count"] == 1


def test_reverse_arrival_produces_identical_evidence_id(client, db):
    """A 先 B 后 与 B 先 A 后 必须得到同一个证据 ID（规范化排序）。"""
    priv, _ = _setup(db)
    a = signed_vote_body(priv, round=7, block_hash=b"\xa1" * 32)
    b = signed_vote_body(priv, round=7, block_hash=b"\xb2" * 32)
    client.post(V1_PATH, json=a)
    id_ab = client.post(V1_PATH, json=b).json()["evidence_id"]

    # 清空后逆序重来
    from app.db import get_pool
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE penalties, votes, evidences, stake_snapshot_entries, "
                "stake_snapshots RESTART IDENTITY CASCADE"
            )
        conn.commit()
    freeze(db, "test-chain-1", 0)

    client.post(V1_PATH, json=b)
    id_ba = client.post(V1_PATH, json=a).json()["evidence_id"]
    assert id_ab == id_ba


def test_malformed_hex_rejected(client, db):
    _setup(db)
    r = client.post(V1_PATH, json={
        "chain_id": "test-chain-1", "validator_pubkey": "zz",
        "round": 1, "block_hash": "aa", "signature": "bb" * 64})
    assert r.status_code == 422
