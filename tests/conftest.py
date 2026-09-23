"""pytest 公共夹具：每条测试使用独立临时库，避免状态串扰。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ibc_teach import relayer
from ibc_teach.app import create_app
from ibc_teach.store import Store


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def store(db_path) -> Store:
    s = Store(db_path)
    yield s
    s.integrity_check()


@pytest.fixture
def client(db_path) -> TestClient:
    app = create_app(db_path)
    with TestClient(app) as c:
        c.store = app.state.store  # type: ignore[attr-defined]
        yield c


# ---------- 高层流程助手（驱动真实 HTTP + 真实签名检查点） ----------


def make_channel(client: TestClient, ordering: str, version: str = "v1") -> dict:
    r = client.post("/channels", json={"ordering": ordering, "version": version})
    assert r.status_code == 201, r.text
    return r.json()


def finalize(client: TestClient, chain: str, *, time_ns: int | None = None) -> dict:
    body = {} if time_ns is None else {"time_ns": time_ns}
    r = client.post(f"/chains/{chain}/blocks", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def send_packet(client: TestClient, *, timeout_height: int = 0,
                timeout_time_ns: int = 0, data_hex: str = "deadbeef",
                amount: int = 100) -> dict:
    r = client.post("/packets/send", json={
        "src_chain": "chain-a", "src_port": "port-a", "src_channel": "channel-0",
        "timeout_height": timeout_height, "timeout_time_ns": timeout_time_ns,
        "data_hex": data_hex, "amount": amount,
    })
    assert r.status_code == 201, r.text
    return r.json()


def deliver(client: TestClient, p: dict, ordering: str) -> dict:
    """源链出块 -> 构造 recv 证明 -> 目的链接收。"""
    src_tip = client.get("/chains").json()
    h_a = next(c["tip_height"] for c in src_tip if c["chain_id"] == "chain-a")
    if h_a == 0:
        finalize(client, "chain-a")
        h_a += 1
    body = relayer.recv_body(client.store, p, h_a)
    r = client.post("/packets/recv", json=body)
    return {"response": r, "json": r.json() if r.content else {}}


def ack(client: TestClient, p: dict, ordering: str, *, h_b: int | None = None) -> dict:
    if h_b is None:
        h_b = next(c["tip_height"] for c in client.get("/chains").json()
                   if c["chain_id"] == "chain-b")
    fn = relayer.ack_body_ordered if ordering == "ordered" else relayer.ack_body_unordered
    body = fn(client.store, p, h_b)
    r = client.post("/packets/ack", json=body)
    return {"response": r, "json": r.json() if r.content else {}}


def timeout_refund(client: TestClient, p: dict, ordering: str,
                   *, h_b: int | None = None) -> dict:
    if h_b is None:
        h_b = next(c["tip_height"] for c in client.get("/chains").json()
                   if c["chain_id"] == "chain-b")
    fn = (relayer.timeout_body_ordered if ordering == "ordered"
          else relayer.timeout_body_unordered)
    body = fn(client.store, p, h_b)
    r = client.post("/packets/timeout", json=body)
    return {"response": r, "json": r.json() if r.content else {}}


def full_happy_path(client: TestClient, ordering: str) -> dict:
    """建通道 -> 发包 -> 源/目的出块 -> 接收 -> 目的出块 -> 确认。"""
    make_channel(client, ordering)
    p = send_packet(client)
    finalize(client, "chain-a")
    d = deliver(client, p, ordering)
    assert d["response"].status_code == 200, d["json"]
    b = finalize(client, "chain-b")
    a = ack(client, p, ordering, h_b=b["height"])
    assert a["response"].status_code == 200, a["json"]
    return {"packet": p, "deliver": d, "ack": a}


def now_ns() -> int:
    return time.time_ns()
