"""超时边界：高度边界与时间戳边界，各测“边界内可收”和“边界点不可收、可退款”。"""

from __future__ import annotations

from ibc_teach import relayer

from .conftest import deliver, finalize, make_channel, send_packet, timeout_refund


# ---------- 高度超时 ----------


def test_height_timeout_boundary_just_before_accepts(client):
    """timeout_height=2：目的链高度 1 时未超时，可接收。"""
    make_channel(client, "ordered")
    p = send_packet(client, timeout_height=2)
    finalize(client, "chain-a")  # a=1
    # 目的链推进到高度 1（< 2）。
    finalize(client, "chain-b")
    d = deliver(client, p, "ordered")
    assert d["response"].status_code == 200, d["json"]


def test_height_timeout_boundary_equal_refuses_and_refunds(client):
    """timeout_height=2：目的链高度 == 2 即超时（严格 < 语义）。

    有序通道：nextSequenceRecv 仍为 1（未消费），源链可超时退款。
    """
    make_channel(client, "ordered")
    p = send_packet(client, timeout_height=2)
    finalize(client, "chain-a")  # a=1（承诺入块，对它做接收证明）
    finalize(client, "chain-b")  # b=1
    finalize(client, "chain-b")  # b=2：到达超时边界

    body = relayer.recv_body(client.store, p, 1)
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 408
    assert r.json()["error"] == "timeout_height_reached"

    # 用 b@2（已超时）的 nextSequenceRecv 非消费证明做超时退款。
    t = timeout_refund(client, p, "ordered", h_b=2)
    assert t["response"].status_code == 200, t["json"]
    assert t["json"]["final_status"] == "TIMED_OUT"
    assert t["json"]["refund_amount"] == p["amount"]

    # 终态后接收仍被拒绝。
    r2 = client.post("/packets/recv", json=relayer.recv_body(client.store, p, 1))
    assert r2.status_code in (408, 409)


def test_height_timeout_unordered_absence_proof(client):
    """无序通道超时：回执缺失的非成员证明。"""
    make_channel(client, "unordered")
    p = send_packet(client, timeout_height=1)
    finalize(client, "chain-a")
    finalize(client, "chain-b")  # b=1 == timeout
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p, 1))
    assert r.status_code == 408
    t = timeout_refund(client, p, "unordered", h_b=1)
    assert t["response"].status_code == 200, t["json"]
    assert t["json"]["final_status"] == "TIMED_OUT"


# ---------- 时间戳超时 ----------


def test_timestamp_timeout_boundary_just_before_accepts(client):
    make_channel(client, "unordered")
    t0 = client.store.tip("chain-b")["time_ns"]
    deadline = t0 + 1_000_000_000  # +1s
    p = send_packet(client, timeout_time_ns=deadline)
    finalize(client, "chain-a")
    # 目的块时间 = deadline - 1ns：未超时。
    finalize(client, "chain-b", time_ns=deadline - 1)
    d = deliver(client, p, "unordered")
    assert d["response"].status_code == 200, d["json"]


def test_timestamp_timeout_boundary_equal_refunds(client):
    make_channel(client, "unordered")
    t0 = client.store.tip("chain-b")["time_ns"]
    deadline = t0 + 2_000_000_000
    p = send_packet(client, timeout_time_ns=deadline)
    finalize(client, "chain-a")
    finalize(client, "chain-b", time_ns=deadline)  # == deadline：超时
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p, 1))
    assert r.status_code == 408
    assert r.json()["error"] == "timeout_timestamp_reached"
    t = timeout_refund(client, p, "unordered", h_b=1)
    assert t["response"].status_code == 200, t["json"]
    assert t["json"]["final_status"] == "TIMED_OUT"


def test_timeout_before_deadline_refused(client):
    """未到超时点不能退款。"""
    make_channel(client, "unordered")
    t0 = client.store.tip("chain-b")["time_ns"]
    p = send_packet(client, timeout_height=100, timeout_time_ns=t0 + 10**12)
    finalize(client, "chain-a")
    finalize(client, "chain-b", time_ns=t0 + 1)
    t = timeout_refund(client, p, "unordered", h_b=1)
    assert t["response"].status_code == 400
    assert "not timed out" in t["json"]["message"]


def test_zero_means_no_timeout(client):
    """timeout_height=0 且 timeout_time_ns=0 表示不设超时。"""
    make_channel(client, "unordered")
    p = send_packet(client)  # 默认 0/0
    finalize(client, "chain-a")
    for _ in range(3):
        finalize(client, "chain-b")
    d = deliver(client, p, "unordered")
    assert d["response"].status_code == 200, d["json"]
