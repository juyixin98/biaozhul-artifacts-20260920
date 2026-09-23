"""通道关闭：发送/接收/确认/退款在 CLOSED 通道上一律拒绝。"""

from __future__ import annotations

from ibc_teach import relayer

from .conftest import (
    ack,
    deliver,
    finalize,
    full_happy_path,
    make_channel,
    send_packet,
    timeout_refund,
)


def test_close_blocks_send_recv_ack_timeout(client):
    make_channel(client, "unordered")
    p = send_packet(client, timeout_height=1)
    finalize(client, "chain-a")

    # 关闭目的端 -> 接收拒绝。
    r = client.post("/chains/chain-b/channels/port-b/channel-0/close")
    assert r.status_code == 200
    body = relayer.recv_body(client.store, p, 1)
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 409 and r.json()["error"] == "channel_closed"

    # 重复关闭 -> 409。
    r = client.post("/chains/chain-b/channels/port-b/channel-0/close")
    assert r.status_code == 409

    # 关闭源端 -> 发送、确认、超时全部拒绝。
    r = client.post("/chains/chain-a/channels/port-a/channel-0/close")
    assert r.status_code == 200
    r = client.post("/packets/send", json={
        "src_chain": "chain-a", "src_port": "port-a", "src_channel": "channel-0",
        "timeout_height": 10, "timeout_time_ns": 0, "data_hex": "ff", "amount": 1,
    })
    assert r.status_code == 409 and r.json()["error"] == "channel_closed"

    finalize(client, "chain-b")  # b=1 到超时点
    t = timeout_refund(client, p, "unordered", h_b=1)
    assert t["response"].status_code == 409 and t["json"]["error"] == "channel_closed"


def test_closed_ordered_channel_blocks_ack(client):
    full_happy_path(client, "ordered")
    p2 = send_packet(client)
    finalize(client, "chain-a")
    d = deliver(client, p2, "ordered")
    assert d["response"].status_code == 200
    finalize(client, "chain-b")
    r = client.post("/chains/chain-a/channels/port-a/channel-0/close")
    assert r.status_code == 200
    a = ack(client, p2, "ordered", h_b=2)
    assert a["response"].status_code == 409 and a["json"]["error"] == "channel_closed"
