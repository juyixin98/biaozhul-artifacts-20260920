# -*- coding: utf-8 -*-
"""超时边界（高度/时间，== 与 > 两个面）、旧检查点拒绝、通道关闭、崩溃恢复。"""
from __future__ import annotations

import os
import time

import pytest

from app import crypto, paths
from app.engine import (
    DEFAULT_TRUSTING_NANOS,
    PKT_IN_FLIGHT,
    PKT_TIMED_OUT,
    Engine,
)
from app.errors import (
    ERR_CHANNEL_CLOSED,
    ERR_NOT_TIMEOUT,
    ERR_PACKET_FINALIZED,
    ERR_PROOF_VALUE,
    ERR_STALE_CHECKPOINT,
    ERR_TIMEOUT,
)
from app.relayer import Relayer
from app.storage import Database

T0 = 1_000_000_000_000  # 固定创世时间（纳秒）
BLOCK = 10 * 1_000_000_000


def _open_and_send(net: Relayer, ordering: str, timeout_height: int, timeout_time: int):
    # network 夹具已建好两条链、双向客户端与连接 conn-1
    net.handshake("chainA", "chainB", ordering=ordering, version="v1")
    pkt = net.send_and_commit(
        "chainA", "channel-0",
        data_hex=b"x".hex(),
        timeout_height=timeout_height,
        timeout_time_nanos=timeout_time,
        amount=50,
    )
    return pkt


# ---------------------------------------------------------- 高度超时两个边界

def test_height_timeout_boundary_equality_and_strictly_before(network: Relayer):
    net = network
    # 先握手（不发包），再以目的链当前高度为基准设置精确超时
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    h0 = net.engine.chain_info("chainB")["height"]
    pkt = net.send_and_commit(
        "chainA", "channel-0", data_hex=b"x".hex(),
        timeout_height=h0 + 2, timeout_time_nanos=0, amount=50,
    )

    net.commit("chainB")  # h0+1 < h0+2：尚未超时
    with pytest.raises(Exception) as ei:
        net.engine.timeout_packet(pkt, net.timeout_proofs(pkt))
    assert ei.value.code == ERR_NOT_TIMEOUT

    net.commit("chainB")  # h0+2 == timeout_height：边界触发
    out = net.engine.timeout_packet(pkt, net.timeout_proofs(pkt))
    assert out["final_status"] == PKT_TIMED_OUT and out["refunded_amount"] == 50


def test_recv_rejected_when_destination_height_reached_timeout(network: Relayer):
    net = network
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    h0 = net.engine.chain_info("chainB")["height"]
    pkt1 = net.send_and_commit(
        "chainA", "channel-0", data_hex=b"x".hex(),
        timeout_height=h0 + 10, timeout_time_nanos=0,
    )
    r = net.relay_recv(pkt1)
    assert r["result"] == "received"

    # 第二个包超时高度设为目的链当前高度+1，再推一块即到达边界
    h_now = net.engine.chain_info("chainB")["height"]
    pkt2 = net.send_and_commit("chainA", "channel-0",
                               data_hex=b"y".hex(), timeout_height=h_now + 1, timeout_time_nanos=0)
    net.commit("chainB")  # h_now+1 == timeout
    proof = net.proof("chainA", paths.packet_commitment_key("channel-0", 2))
    with pytest.raises(Exception) as ei:
        net.engine.recv_packet(pkt2, proof)
    assert ei.value.code == ERR_TIMEOUT


# ---------------------------------------------------------- 时间超时两个边界

def test_timestamp_timeout_boundary_equality(network: Relayer):
    net = network
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    t0 = net.engine.chain_info("chainB")["time_nanos"]  # 握手后目的链时间
    limit = t0 + 2 * BLOCK
    pkt = net.send_and_commit(
        "chainA", "channel-0", data_hex=b"x".hex(),
        timeout_height=0, timeout_time_nanos=limit, amount=50,
    )

    net.commit("chainB")  # t0+10s：尚未超时
    with pytest.raises(Exception) as ei:
        net.engine.timeout_packet(pkt, net.timeout_proofs(pkt))
    assert ei.value.code == ERR_NOT_TIMEOUT

    net.commit("chainB", time_nanos=limit)  # t == limit：边界触发
    out = net.engine.timeout_packet(pkt, net.timeout_proofs(pkt))
    assert out["final_status"] == PKT_TIMED_OUT


def test_recv_rejected_on_timestamp_boundary(network: Relayer):
    net = network
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    t0 = net.engine.chain_info("chainB")["time_nanos"]
    limit = t0 + BLOCK  # 一个区块后恰为边界
    pkt = net.send_and_commit(
        "chainA", "channel-0", data_hex=b"x".hex(),
        timeout_height=0, timeout_time_nanos=limit,
    )
    net.commit("chainB", time_nanos=limit)
    proof = net.proof("chainA", paths.packet_commitment_key("channel-0", 1))
    with pytest.raises(Exception) as ei:
        net.engine.recv_packet(pkt, proof)
    assert ei.value.code == ERR_TIMEOUT


# ---------------------------------------------------------- 有序通道超时

def test_ordered_timeout_closes_both_channels_over_time(network: Relayer):
    net = network
    net.handshake("chainA", "chainB", ordering="ORDERED", version="v1")
    t0 = net.engine.chain_info("chainB")["time_nanos"]
    pkt = net.send_and_commit(
        "chainA", "channel-0", data_hex=b"x".hex(),
        timeout_height=0, timeout_time_nanos=t0 + 2 * BLOCK, amount=50,
    )
    # 有序超时证明基于 nextSeqRecv 未推进
    net.commit("chainB", time_nanos=t0 + BLOCK)
    with pytest.raises(Exception) as ei:
        net.engine.timeout_packet(
            pkt, {"proof_unreceived": net.proof("chainB", paths.next_seq_recv_key("channel-0"))}
        )
    assert ei.value.code == ERR_NOT_TIMEOUT
    net.commit("chainB", time_nanos=t0 + 2 * BLOCK)
    proofs = {"proof_unreceived": net.proof("chainB", paths.next_seq_recv_key("channel-0"))}
    out = net.engine.timeout_packet(pkt, proofs)
    assert out["final_status"] == PKT_TIMED_OUT
    # 有序通道超时连带关闭源端
    assert net.engine.channel_info("chainA", "channel-0")["state"] == "CLOSED"


# ---------------------------------------------------------- 旧检查点

def test_old_lower_height_checkpoint_rejected(network: Relayer):
    net = network
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    pkt = net.send_and_commit("chainA", "channel-0",
                              data_hex=b"old".hex(), timeout_height=100, timeout_time_nanos=0)
    h_hi = net.engine.chain_info("chainA")["height"]
    proof_hi = net.proof("chainA", paths.packet_commitment_key("channel-0", 1))
    assert net.engine.recv_packet(pkt, proof_hi)["result"] == "received"  # 客户端信任推进到 h_hi
    net.commit("chainB")

    # 再取一个高度更低的“旧检查点”（源链倒数第二个区块）伪造重放
    old_row = net.engine.db.query_one(
        "SELECT * FROM checkpoints WHERE chain_id=? AND height=?",
        ("chainA", h_hi - 1),
    )
    old_cp = net.engine._checkpoint_dict(net.engine.db.connect(), old_row)
    proof_old = dict(proof_hi)
    proof_old["checkpoint"] = old_cp
    with pytest.raises(Exception) as ei:
        net.engine.recv_packet(pkt, proof_old)
    assert ei.value.code == ERR_STALE_CHECKPOINT


def test_checkpoint_beyond_trusting_period_rejected(network: Relayer):
    """用很短的信任期建链：检查点时间相对本地当前时间太旧 -> 拒绝。"""
    eng = network.engine
    eng.create_chain("chainX", revision_number=1, genesis_time_nanos=T0)
    eng.create_chain("chainY", revision_number=1, genesis_time_nanos=T0)
    short = 60 * 1_000_000_000  # 60 秒信任期
    eng.create_client("chainY", "chainX", trusting_period_nanos=short)
    eng.create_client("chainX", "chainY", trusting_period_nanos=short)
    eng.create_connection("conn-xy", "chainX", "chainY")
    rly = Relayer(eng)

    eng.channel_open_init("chainX", "channel-0", "conn-xy", "UNORDERED", "v1", "channel-0")
    rly.commit("chainX")  # T0+10s
    proof = rly.proof("chainX", paths.channel_key("channel-0"))

    # Y 链时间推进到信任期之外
    rly.commit("chainY", time_nanos=T0 + 10 * BLOCK)
    with pytest.raises(Exception) as ei:
        eng.channel_open_try(
            "chainY", "channel-0", "conn-xy", "UNORDERED", "v1", "channel-0", "v1", proof
        )
    assert ei.value.code == ERR_STALE_CHECKPOINT


def test_missing_checkpoint_and_bad_signature_rejected_on_recv(network: Relayer):
    net = network
    pkt = _open_and_send(net, "UNORDERED", 100, 0)
    good = net.proof("chainA", paths.packet_commitment_key("channel-0", 1))
    no_cp = {k: v for k, v in good.items() if k != "checkpoint"}
    with pytest.raises(Exception) as ei:
        net.engine.recv_packet(pkt, no_cp)
    assert ei.value.code == "BAD_PROOF_STRUCTURE"

    sig = bytearray(bytes.fromhex(good["checkpoint"]["signature"]))
    sig[10] ^= 0x01
    bad = dict(good)
    cp = dict(good["checkpoint"])
    cp["signature"] = sig.hex()
    bad["checkpoint"] = cp
    with pytest.raises(Exception) as ei:
        net.engine.recv_packet(pkt, bad)
    assert ei.value.code == "INVALID_CHECKPOINT"


# ---------------------------------------------------------- 通道关闭

def test_closed_channel_blocks_send_and_recv_but_closed_proof_allows_timeout(network: Relayer):
    net = network
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    t0 = net.engine.chain_info("chainB")["time_nanos"]
    pkt = net.send_and_commit(
        "chainA", "channel-0", data_hex=b"x".hex(),
        timeout_height=0, timeout_time_nanos=t0 + 3 * BLOCK, amount=50,
    )
    # 关闭目的端通道并提交（同时推进到超时阈值之后）
    net.engine.channel_close("chainB", "channel-0")
    net.commit("chainB", time_nanos=t0 + 3 * BLOCK)

    # 关闭后不能再收包
    proof = net.proof("chainA", paths.packet_commitment_key("channel-0", 1))
    with pytest.raises(Exception) as ei:
        net.engine.recv_packet(pkt, proof)
    assert ei.value.code == ERR_CHANNEL_CLOSED

    # 用 CLOSED 通道证明在源端完成超时退款（无需非成员证明）
    closed_proof = {"proof_channel_closed": net.proof("chainB", paths.channel_key("channel-0"))}
    out = net.engine.timeout_packet(pkt, closed_proof)
    assert out["final_status"] == PKT_TIMED_OUT


def test_source_cannot_send_on_closed_channel(network: Relayer):
    net = network
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    net.engine.channel_close("chainA", "channel-0")
    with pytest.raises(Exception) as ei:
        net.engine.send_packet(
            "chainA", "channel-0", b"z".hex(), timeout_height=99, timeout_time_nanos=0
        )
    assert ei.value.code in (ERR_CHANNEL_CLOSED, "CHANNEL_INVALID_STATE")


# ---------------------------------------------------------- 崩溃恢复

def test_crash_recovery_rebuilds_state_and_preserves_terminal(tmp_db):
    """模拟进程崩溃：用同一数据库文件新建 Engine（重启），
    状态必须完整保留，完整性校验通过，终态包不能被二次终结。"""
    eng = Engine(Database(tmp_db))
    net = Relayer(eng)
    net.setup_two_chains("chainA", "chainB", genesis_time_nanos=T0)
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    hb = eng.chain_info("chainB")["height"]
    pkt = net.send_and_commit("chainA", "channel-0",
                              data_hex=b"x".hex(), timeout_height=hb + 1,
                              timeout_time_nanos=0, amount=50)
    net.commit("chainB")  # 到达超时高度
    eng.timeout_packet(pkt, net.timeout_proofs(pkt))
    net.commit("chainA")
    before = eng.verify_integrity()
    assert all(c["checkpoints"] >= 3 for c in before["chains"])

    # “重启”：丢弃 Engine 与连接，重新打开同一 DB 文件
    del eng, net
    eng2 = Engine(Database(tmp_db))
    report = eng2.verify_integrity()  # 启动完整性验证必须通过
    assert report["chains"]
    for c in report["chains"]:
        assert c["uncommitted_state"] is False

    pkt_info = eng2.packet_info("chainA", "channel-0", 1)
    assert pkt_info["status"] == PKT_TIMED_OUT
    # 终态持久化：重启后重复超时仍被拒
    net2 = Relayer(eng2)
    proofs = {"proof_unreceived": net2.proof("chainB", paths.packet_receipt_key("channel-0", 1))}
    with pytest.raises(Exception) as ei:
        eng2.timeout_packet(pkt_info, proofs)
    assert ei.value.code in (ERR_PACKET_FINALIZED, ERR_PROOF_VALUE)


def test_integrity_check_detects_tampered_checkpoint(tmp_db):
    eng = Engine(Database(tmp_db))
    net = Relayer(eng)
    net.setup_two_chains("chainA", "chainB", genesis_time_nanos=T0)
    net.commit("chainA")
    cp = eng.latest_checkpoint("chainA")

    # 直接篡改底层 DB 里的签名字节（模拟存储被篡改）
    conn = eng.db.connect()
    bad = bytes(64).hex()
    conn.execute(
        "UPDATE checkpoints SET signature=? WHERE chain_id=? AND height=?",
        (bad, "chainA", cp["height"]),
    )
    conn.commit() if False else None  # autocommit 模式已直接落盘

    eng2 = Engine(Database(tmp_db))
    with pytest.raises(Exception) as ei:
        eng2.verify_integrity()
    assert ei.value.code == "INTEGRITY_FAILURE"
