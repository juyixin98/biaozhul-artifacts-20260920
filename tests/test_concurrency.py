"""并发归并测试：同一验证者同一轮的冲突票/重复票并发提交。

直接并发调用归并领域函数（每个线程独立连接），覆盖最真实的竞态窗口：
- 两张不同内容的票同时到达，只允许产生一份证据、一条惩罚；
- 相同内容的票同时到达，只允许落一张票、无证据。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import slashing
from tests.conftest import make_chain, make_validator


def _key(seed: int = 1):
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def _ingest(db, body):
    """独立连接执行一次归并 + 处罚（与 HTTP 端点流程一致）；序列化冲突时重试。"""
    with db.connection() as conn:
        try:
            out = slashing.ingest_vote(
                conn,
                chain_id=body["chain_id"],
                pubkey=bytes.fromhex(body["validator_pubkey"]),
                round=body["round"],
                block_hash=bytes.fromhex(body["block_hash"]),
                signature=bytes.fromhex(body["signature"]),
                raw=body,
            )
            conn.commit()
        except psycopg.errors.SerializationFailure:
            conn.rollback()
            return "serialization_retry"
    if out["result"] == "new_evidence":
        with db.connection() as conn:
            slashing.apply_penalty(conn, evidence_id=bytes.fromhex(out["evidence_id"]))
            conn.commit()
    return out["result"]


def test_concurrent_conflicting_votes_single_evidence(db):
    make_chain(db, "test-chain-1", 10)
    priv = _key(1)
    make_validator(db, priv, "test-chain-1", "alice", 100_000_000)
    with db.connection() as conn:
        slashing.freeze_epoch(conn, "test-chain-1", 0)
        conn.commit()

    from app.crypto import derive_pubkey, sign_vote
    pub = derive_pubkey(priv)

    def body(block_hash):
        sig = sign_vote(priv, chain_id="test-chain-1", validator_pubkey=pub,
                        round=7, block_hash=block_hash)
        return {
            "chain_id": "test-chain-1",
            "validator_pubkey": pub.hex(),
            "round": 7,
            "block_hash": block_hash.hex(),
            "signature": sig.hex(),
        }

    b1 = body(b"\x01" * 32)
    b2 = body(b"\x02" * 32)
    results = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_ingest, db, b1), ex.submit(_ingest, db, b2)]
        results = [f.result() for f in futs]

    assert sorted(results) == sorted(["first", "new_evidence"])

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM evidences")
            ev_count = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM penalties")
            pen_count = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM votes")
            vote_count = cur.fetchone()["n"]
    assert ev_count == 1
    assert pen_count == 1
    assert vote_count == 2

    # 恢复必须是幂等空操作
    with db.connection() as conn:
        rec = slashing.recover_pending(conn)
        conn.commit()
    assert rec["recovered"] == []


def test_repeated_concurrent_conflicting_rounds(db):
    """多轮重复压测：每轮两张冲突票，永远证据数 == 惩罚数 == 轮数。"""
    make_chain(db, "test-chain-1", 10)
    priv = _key(1)
    make_validator(db, priv, "test-chain-1", "alice", 100_000_000)
    with db.connection() as conn:
        slashing.freeze_epoch(conn, "test-chain-1", 0)
        conn.commit()

    from app.crypto import derive_pubkey, sign_vote
    pub = derive_pubkey(priv)

    def body(round, block_hash):
        sig = sign_vote(priv, chain_id="test-chain-1", validator_pubkey=pub,
                        round=round, block_hash=block_hash)
        return {
            "chain_id": "test-chain-1",
            "validator_pubkey": pub.hex(),
            "round": round,
            "block_hash": block_hash.hex(),
            "signature": sig.hex(),
        }

    rounds = list(range(0, 9))
    bodies = [(body(r, b"\xaa" * 32), body(r, b"\xbb" * 32)) for r in rounds]
    flat = [x for pair in bodies for x in pair]

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda b: _ingest(db, b), flat))

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM evidences")
            ev = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM penalties")
            pen = cur.fetchone()["n"]
    assert ev == 9
    assert pen == 9


def test_concurrent_identical_votes_no_evidence(db):
    make_chain(db, "test-chain-1", 10)
    priv = _key(1)
    make_validator(db, priv, "test-chain-1", "alice", 100_000_000)
    with db.connection() as conn:
        slashing.freeze_epoch(conn, "test-chain-1", 0)
        conn.commit()

    from app.crypto import derive_pubkey, sign_vote
    pub = derive_pubkey(priv)
    sig = sign_vote(priv, chain_id="test-chain-1", validator_pubkey=pub,
                    round=7, block_hash=b"\x77" * 32)
    body = {
        "chain_id": "test-chain-1",
        "validator_pubkey": pub.hex(),
        "round": 7,
        "block_hash": (b"\x77" * 32).hex(),
        "signature": sig.hex(),
    }
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda _: _ingest(db, body), range(4)))

    # 一张 first，其余 duplicate；绝不能有证据
    assert results.count("first") == 1
    assert results.count("duplicate") == 3
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM evidences")
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT count(*) AS n FROM votes")
            assert cur.fetchone()["n"] == 1
