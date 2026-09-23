"""通道绑定：顺序模式、对端、版本必须一致；跨通道证明不得复用。"""

from __future__ import annotations

import pytest

from ibc_teach import relayer

from .conftest import deliver, finalize, make_channel, send_packet


def test_packet_counterparty_mismatch_rejected(client):
    make_channel(client, "unordered", version="v1")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    # 篡改包的目的端口，使其与目的链本地通道绑定不符。
    body["packet"]["dst_port"] = "port-other"
    r = client.post("/packets/recv", json=body)
    assert r.status_code in (400, 404)


def test_checkpoint_wrong_source_chain_rejected(client):
    make_channel(client, "unordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    # 目的端收到“chain-b 自己”的检查点冒充源链：chain_id 不匹配。
    body["checkpoint"]["chain_id"] = "chain-b"
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 400


def test_version_mismatch_prevents_cross_channel_replay(client, store):
    """同端口上先建 v1 通道，关闭后再建 v2 通道（不同 channel id）；
    v1 通道的承诺证明不能在 v2 通道端点上被接受。"""
    from ibc_teach.store import ErrProof

    store.create_channel(ordering="unordered", version="v1",
                         channel_a="channel-0", channel_b="channel-0")
    p = store.send_packet(
        src_chain="chain-a", src_port="port-a", src_channel="channel-0",
        timeout_height=0, timeout_time_ns=0, data_hex="01", amount=1,
    )
    store.finalize_block("chain-a")
    store.close_channel("chain-a", "port-a", "channel-0")
    store.close_channel("chain-b", "port-b", "channel-0")

    # 新通道（version v2，新 id）。
    store.create_channel(ordering="unordered", version="v2",
                         channel_a="channel-1", channel_b="channel-1")

    # 拿 channel-0 的旧承诺证明，伪称是 channel-1 的包 -> 对端绑定不匹配。
    body = relayer.recv_body(store, p, 1)
    body["packet"]["dst_channel"] = "channel-1"
    with pytest.raises(ErrProof):
        store.recv_packet(body["packet"], body["checkpoint"], body["proof"])


def test_unknown_channel_404(client):
    make_channel(client, "ordered")
    p = send_packet(client)
    finalize(client, "chain-a")
    body = relayer.recv_body(client.store, p, 1)
    body["packet"]["dst_channel"] = "channel-999"
    r = client.post("/packets/recv", json=body)
    assert r.status_code == 404
