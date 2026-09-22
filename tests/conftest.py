"""pytest 全局夹具：在导入应用前锁定测试库，每个用例前清空业务表。"""
from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://slasher:slasher_pw@localhost:5432/slasher_test",
)
os.environ.pop("SLASHER_CRASH_AFTER_EVIDENCE", None)

import pytest
from fastapi.testclient import TestClient

from app import slashing
from app.db import close_pool, get_pool, init_pool, init_schema

TABLES = [
    "penalties",
    "votes",
    "evidences",
    "stake_snapshot_entries",
    "stake_snapshots",
    "validator_power",
    "validators",
    "chains",
]


@pytest.fixture(scope="session")
def _schema():
    init_pool(min_size=1, max_size=10)
    init_schema()
    yield
    close_pool()


@pytest.fixture(autouse=True)
def _clean(_schema):
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
        conn.commit()
    yield


@pytest.fixture()
def client():
    # 每个用例独立 TestClient；其 lifespan 关闭连接池后 get_pool() 会自动重建。
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    return get_pool()


# ------------------------------------------------------------- 测试辅助


def make_chain(db, chain_id="test-chain-1", epoch_length=10):
    with db.connection() as conn:
        slashing.create_chain(conn, chain_id, epoch_length)
        conn.commit()


def make_validator(db, priv, chain_id="test-chain-1", moniker="", power=None):
    from app.crypto import derive_pubkey

    pub = derive_pubkey(priv)
    with db.connection() as conn:
        slashing.register_validator(conn, chain_id, pub, moniker)
        if power is not None:
            slashing.set_power(conn, chain_id, pub, power)
        conn.commit()
    return pub


def freeze(db, chain_id="test-chain-1", epoch=0):
    with db.connection() as conn:
        out = slashing.freeze_epoch(conn, chain_id, epoch)
        conn.commit()
    return out


def signed_vote_body(priv, *, chain_id="test-chain-1", round=7, block_hash=b"\xa1" * 32):
    from app.crypto import derive_pubkey, sign_vote

    pub = derive_pubkey(priv)
    sig = sign_vote(
        priv, chain_id=chain_id, validator_pubkey=pub,
        round=round, block_hash=block_hash,
    )
    return {
        "chain_id": chain_id,
        "validator_pubkey": pub.hex(),
        "round": round,
        "block_hash": block_hash.hex(),
        "signature": sig.hex(),
    }
