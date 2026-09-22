"""epoch 边界：快照冻结、边界轮次归属、后续委托不能改变历史惩罚基数。"""
from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import slashing
from app.crypto import derive_pubkey
from tests.conftest import make_chain, make_validator, signed_vote_body

V1_PATH = "/api/v1/chains/test-chain-1/votes"


def _key(seed: int = 1):
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def _setup(db, power):
    make_chain(db, "test-chain-1", epoch_length=10)
    priv = _key(1)
    pub = make_validator(db, priv, "test-chain-1", "alice", power)
    return priv, pub


def test_epoch_assignment_at_boundaries(db):
    assert slashing.epoch_of(0, 10) == 0
    assert slashing.epoch_of(9, 10) == 0
    assert slashing.epoch_of(10, 10) == 1
    assert slashing.epoch_of(19, 10) == 1
    assert slashing.epoch_of(20, 10) == 2


def test_boundary_round_uses_next_epoch_snapshot(client, db):
    """round 10 属于 epoch 1；epoch 1 快照未冻结时处罚挂起，冻结后补罚。"""
    priv, pub = _setup(db, 100_000_000)
    with db.connection() as conn:
        slashing.freeze_epoch(conn, "test-chain-1", 0)
        conn.commit()

    a = signed_vote_body(priv, round=10, block_hash=b"\xa1" * 32)
    b = signed_vote_body(priv, round=10, block_hash=b"\xb2" * 32)
    client.post(V1_PATH, json=a)
    out = client.post(V1_PATH, json=b).json()
    assert out["result"] == "new_evidence"
    assert out["penalty"]["deferred"] is True

    # 证据处于 pending
    ev = client.get(f"/api/v1/evidences/{out['evidence_id']}").json()
    assert ev["evidence"]["status"] == "pending"
    assert ev["penalty"] is None

    # 冻结 epoch 1 后恢复 -> 引用 epoch 1 快照
    r = client.post("/api/v1/chains/test-chain-1/epochs/1/freeze").json()
    assert out["evidence_id"] in r["recovery"]["recovered"]

    detail = client.get(f"/api/v1/evidences/{out['evidence_id']}").json()
    assert detail["evidence"]["status"] == "punished"
    assert detail["penalty"]["snapshot_epoch"] == 1


def test_round_9_still_epoch_zero(client, db):
    priv, _ = _setup(db, 50_000_000)
    with db.connection() as conn:
        slashing.freeze_epoch(conn, "test-chain-1", 0)
        conn.commit()
    a = signed_vote_body(priv, round=9, block_hash=b"\x01" * 32)
    b = signed_vote_body(priv, round=9, block_hash=b"\x02" * 32)
    client.post(V1_PATH, json=a)
    out = client.post(V1_PATH, json=b).json()
    assert out["penalty"]["snapshot_epoch"] == 0
    assert out["penalty"]["slashed_power"] == 500_000  # 50 * 1%


def test_later_delegation_does_not_change_historical_base(client, db):
    """epoch 0 双签按 epoch 0 快照基数处罚；事后增加委托不影响已处罚，
    新快照也不改变历史惩罚引用的基数。"""
    priv, pub = _setup(db, 100_000_000)  # 冻结前 100
    with db.connection() as conn:
        slashing.freeze_epoch(conn, "test-chain-1", 0)
        conn.commit()

    a = signed_vote_body(priv, round=5, block_hash=b"\x01" * 32)
    b = signed_vote_body(priv, round=5, block_hash=b"\x02" * 32)
    client.post(V1_PATH, json=a)
    out = client.post(V1_PATH, json=b).json()
    eid = out["evidence_id"]
    assert out["penalty"]["base_power"] == 100_000_000
    assert out["penalty"]["slashed_power"] == 1_000_000

    # 事后把当前权益改成 999（新委托/解绑），并冻结 epoch 1
    with db.connection() as conn:
        slashing.set_power(conn, "test-chain-1", pub, 999)
        conn.commit()
    client.post("/api/v1/chains/test-chain-1/epochs/1/freeze")

    # 历史惩罚记录不变，仍引用 epoch 0 的 100_000_000
    detail = client.get(f"/api/v1/evidences/{eid}").json()
    assert detail["penalty"]["snapshot_epoch"] == 0
    assert detail["penalty"]["base_power"] == 100_000_000
    assert detail["penalty"]["slashed_power"] == 1_000_000

    # epoch 0 快照明细本身也没有被改写
    snap = client.get("/api/v1/chains/test-chain-1/epochs/0/snapshot").json()
    entry = [e for e in snap["entries"]
             if e["validator_pubkey"] == pub.hex()][0]
    assert entry["power"] == 100_000_000


def test_snapshot_freeze_is_idempotently_rejected(client, db):
    _setup(db, 1000)
    client.post("/api/v1/chains/test-chain-1/epochs/0/freeze")
    r = client.post("/api/v1/chains/test-chain-1/epochs/0/freeze")
    assert r.status_code == 409


def test_empty_snapshot_rejected(client, db):
    make_chain(db, "test-chain-1", 10)
    priv = _key(3)
    make_validator(db, priv, "test-chain-1", "poor", 0)
    r = client.post("/api/v1/chains/test-chain-1/epochs/0/freeze")
    assert r.status_code == 409


def test_power_set_on_unknown_validator_404(client, db):
    make_chain(db, "test-chain-1", 10)
    pub = derive_pubkey(_key(7))
    r = client.put(
        f"/api/v1/chains/test-chain-1/validators/{pub.hex()}/power",
        json={"power": 5},
    )
    assert r.status_code == 404
