"""通道语义：顺序通道遇缺口等待、按序确认；无序通道逐包去重。"""

from __future__ import annotations

from ibc_teach import relayer

from .conftest import ack, deliver, finalize, make_channel, send_packet


def test_ordered_gap_then_unblock_in_order(client):
    make_channel(client, "ordered")
    p1 = send_packet(client)
    p2 = send_packet(client)
    p3 = send_packet(client)
    finalize(client, "chain-a")  # 承诺 1/2/3 全部入块

    # 直接投 seq 2：缺口，必须等待。
    body2 = relayer.recv_body(client.store, p2, 1)
    r = client.post("/packets/recv", json=body2)
    assert r.status_code == 409
    assert r.json()["error"] == "sequence_gap"
    assert "gap" in r.json()["message"]

    # seq 3 同样等待。
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p3, 1))
    assert r.status_code == 409 and r.json()["error"] == "sequence_gap"

    # seq 1 到达后解锁。
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p1, 1))
    assert r.status_code == 200, r.text
    # seq 2 随后可处理（检查点无需更新，同一源证明仍有效）。
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p2, 1))
    assert r.status_code == 200, r.text
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p3, 1))
    assert r.status_code == 200, r.text

    # nextSequenceRecv 推进到 4。
    chs = {c["channel_id"]: c for c in client.get("/chains/chain-b/channels").json()}
    assert chs["channel-0"]["next_seq_recv"] == 4


def test_ordered_ack_must_follow_sequence(client):
    make_channel(client, "ordered")
    p1 = send_packet(client)
    p2 = send_packet(client)
    finalize(client, "chain-a")
    assert deliver(client, p1, "ordered")["response"].status_code == 200
    assert deliver(client, p2, "ordered")["response"].status_code == 200
    finalize(client, "chain-b")

    # 跳过 seq1 直接确认 seq2：有序通道不允许。
    body = relayer.ack_body_ordered(client.store, p2, 1)
    r = client.post("/packets/ack", json=body)
    assert r.status_code == 409 and r.json()["error"] == "sequence_gap"

    # 按序确认成功。
    r = client.post("/packets/ack", json=relayer.ack_body_ordered(client.store, p1, 1))
    assert r.status_code == 200, r.text
    r = client.post("/packets/ack", json=relayer.ack_body_ordered(client.store, p2, 1))
    assert r.status_code == 200, r.text


def test_unordered_arbitrary_order_and_dedup(client):
    make_channel(client, "unordered")
    p1 = send_packet(client)
    p2 = send_packet(client)
    p3 = send_packet(client)
    finalize(client, "chain-a")

    # 乱序投递：3 -> 1 都立即接受（无缺口概念）。
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p3, 1))
    assert r.status_code == 200, r.text
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p1, 1))
    assert r.status_code == 200, r.text

    # 同一包重复投递：去重拒绝（幂等安全）。
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p3, 1))
    assert r.status_code == 409 and r.json()["error"] == "already_exists"
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p1, 1))
    assert r.status_code == 409 and r.json()["error"] == "already_exists"

    # seq2 到达，仍然成功。
    r = client.post("/packets/recv", json=relayer.recv_body(client.store, p2, 1))
    assert r.status_code == 200, r.text


def test_ordered_happy_path_statuses(client):
    make_channel(client, "ordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    d = deliver(client, p, "ordered")
    assert d["response"].status_code == 200
    finalize(client, "chain-b")
    a = ack(client, p, "ordered")
    assert a["response"].status_code == 200
    assert a["json"]["final_status"] == "ACKED"
    rows = {row["sequence"]: row for row in client.get("/packets").json()}
    assert rows[p["sequence"]]["status"] == "ACKED"
