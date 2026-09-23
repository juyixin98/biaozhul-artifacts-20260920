"""中继者辅助函数（教学）：从 Store 读取已签名检查点与状态证明，
组装 recv/ack/timeout 请求体。真实 IBC 中这些由链下 relayer 跨进程完成，
这里直接在同库上读取，密码学验证一步不少。
"""

from __future__ import annotations

from typing import Any

from . import store as st


def packet_msg(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "src_chain": p["src_chain"],
        "src_port": p["src_port"],
        "src_channel": p["src_channel"],
        "dst_chain": p["dst_chain"],
        "dst_port": p["dst_port"],
        "dst_channel": p["dst_channel"],
        "sequence": p["sequence"],
        "timeout_height": p["timeout_height"],
        "timeout_time_ns": p["timeout_time_ns"],
        "data_hex": p["data_hex"],
        "amount": p["amount"],
    }


def recv_body(store: st.Store, p: dict[str, Any], proof_height: int) -> dict[str, Any]:
    """RecvPacket：源链包承诺的成员证明。"""
    key = st.commitment_key(p["src_port"], p["src_channel"], p["sequence"])
    pr = store.proof(p["src_chain"], proof_height, key)
    assert pr["exists"], "commitment missing at given height"
    return {
        "packet": packet_msg(p),
        "checkpoint": pr["checkpoint"],
        "proof": {"steps": pr["proof"]["steps"]},
    }


def ack_body_unordered(store: st.Store, p: dict[str, Any], proof_height: int) -> dict[str, Any]:
    """无序 Ack：目的链回执成员证明。"""
    key = st.receipt_key(p["dst_port"], p["dst_channel"], p["sequence"])
    pr = store.proof(p["dst_chain"], proof_height, key)
    assert pr["exists"], "receipt missing at given height"
    return {
        "packet": packet_msg(p),
        "checkpoint": pr["checkpoint"],
        "proof": {"steps": pr["proof"]["steps"]},
    }


def ack_body_ordered(store: st.Store, p: dict[str, Any], proof_height: int) -> dict[str, Any]:
    """有序 Ack：目的链 nextSequenceRecv > seq 的成员证明。"""
    return _nsr_body(store, p, proof_height)


def timeout_body_unordered(store: st.Store, p: dict[str, Any], proof_height: int) -> dict[str, Any]:
    """无序 Timeout：目的链回执缺失的非成员证明。"""
    key = st.receipt_key(p["dst_port"], p["dst_channel"], p["sequence"])
    pr = store.proof(p["dst_chain"], proof_height, key)
    assert not pr["exists"], "receipt unexpectedly present"
    return {
        "packet": packet_msg(p),
        "checkpoint": pr["checkpoint"],
        "proof": {"steps": pr["proof"]["steps"]},
    }


def timeout_body_ordered(store: st.Store, p: dict[str, Any], proof_height: int) -> dict[str, Any]:
    """有序 Timeout：nextSequenceRecv <= seq 的成员证明。"""
    return _nsr_body(store, p, proof_height)


def _nsr_body(store: st.Store, p: dict[str, Any], proof_height: int) -> dict[str, Any]:
    key = st.next_seq_recv_key(p["dst_port"], p["dst_channel"])
    pr = store.proof(p["dst_chain"], proof_height, key)
    assert pr["exists"], "nextSequenceRecv key missing"
    return {
        "packet": packet_msg(p),
        "checkpoint": pr["checkpoint"],
        "proof": {
            "steps": pr["proof"]["steps"],
            "value_hex": pr["value_hex"],
        },
    }
