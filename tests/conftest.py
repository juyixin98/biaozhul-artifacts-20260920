"""测试基座：使用独立的 asset_recon_test 数据库，每个用例前清空所有表。"""
import os

os.environ["RECON_DATABASE_URL"] = (
    "postgresql+psycopg2://recon:recon@127.0.0.1:5432/asset_recon_test"
)

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def create_schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def clean_tables():
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------ 测试数据构造

def make_event(chain, tx, log_index, block, etype, message_id,
               source_chain="chainA", contract="0xCONTRACT", token_id="1",
               amount=100, dest_chain=None, sender="0xSENDER", recipient="0xRECIPIENT"):
    return {
        "chain": chain, "tx_hash": tx, "log_index": log_index,
        "block_number": block, "event_type": etype, "message_id": message_id,
        "source_chain": source_chain, "origin_contract": contract,
        "token_id": token_id, "amount": amount,
        "sender": sender, "recipient": recipient, "dest_chain": dest_chain,
    }


def lock(msg="msg-1", block=100, tx="0xlock1", chain="chainA", dest_chain="chainB", **kw):
    return make_event(chain, tx, 0, block, "LOCK", msg, dest_chain=dest_chain, **kw)


def mint(msg="msg-1", block=200, tx="0xmint1", chain="chainB", **kw):
    return make_event(chain, tx, 0, block, "MINT", msg, **kw)


def burn(msg="msg-1", block=300, tx="0xburn1", chain="chainB", dest_chain="chainA", **kw):
    return make_event(chain, tx, 0, block, "BURN", msg, dest_chain=dest_chain, **kw)


def release(msg="msg-1", block=400, tx="0xrelease1", chain="chainA", **kw):
    return make_event(chain, tx, 0, block, "RELEASE", msg, **kw)


def ingest(client, events):
    r = client.post("/events/batch", json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


def set_heads(client, a=None, b=None, confirmations=2):
    if a is not None:
        r = client.put("/chains/chainA/head", json={"height": a, "confirmations": confirmations})
        assert r.status_code == 200, r.text
    if b is not None:
        r = client.put("/chains/chainB/head", json={"height": b, "confirmations": confirmations})
        assert r.status_code == 200, r.text


def reconcile(client, timeout=None):
    body = {} if timeout is None else {"pairing_timeout_blocks": timeout}
    r = client.post("/reconcile", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def anomalies(client, snapshot_id):
    r = client.get(f"/snapshots/{snapshot_id}")
    assert r.status_code == 200, r.text
    return r.json()["anomalies"]
