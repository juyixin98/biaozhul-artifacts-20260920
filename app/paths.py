# -*- coding: utf-8 -*-
"""IBC 风格的状态键（store path）。

教学项目把连接/客户端子树也简化成确定的 ASCII 路径，
数据包相关的键则采用 IBC v1 规范的真实路径格式。
"""
from __future__ import annotations

from .encoding import u64be

PORT = "transfer"


def channel_key(channel_id: str, port: str = PORT) -> bytes:
    return f"channel/end/{port}/{channel_id}".encode()


def next_seq_recv_key(channel_id: str, port: str = PORT) -> bytes:
    return f"channel/nextSeqRecv/{port}/{channel_id}".encode()


def next_seq_send_key(channel_id: str, port: str = PORT) -> bytes:
    return f"channel/nextSeqSend/{port}/{channel_id}".encode()


def packet_commitment_key(channel_id: str, sequence: int, port: str = PORT) -> bytes:
    # IBC v1: commitments/ports/{port}/channels/{channel}/sequences/{seq}
    return f"commitments/ports/{port}/channels/{channel_id}/sequences/{sequence}".encode()


def packet_receipt_key(channel_id: str, sequence: int, port: str = PORT) -> bytes:
    # IBC v1: receipts/ports/{port}/channels/{channel}/sequences/{seq}
    return f"receipts/ports/{port}/channels/{channel_id}/sequences/{sequence}".encode()


def packet_ack_key(channel_id: str, sequence: int, port: str = PORT) -> bytes:
    # IBC v1: acks/ports/{port}/channels/{channel}/sequences/{seq}
    return f"acks/ports/{port}/channels/{channel_id}/sequences/{sequence}".encode()


def channel_value(
    state: str,
    ordering: str,
    version: str,
    remote_channel: str,
    remote_port: str,
    conn_id: str,
) -> bytes:
    from .encoding import canonical_json

    return canonical_json(
        {
            "state": state,
            "ordering": ordering,
            "version": version,
            "connection": conn_id,
            "counterparty_channel": remote_channel,
            "counterparty_port": remote_port,
        }
    )


def seq_value(seq: int) -> bytes:
    return u64be(seq)
