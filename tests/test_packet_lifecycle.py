# -*- coding: utf-8 -*-
"""数据包生命周期：发送 -> 接收 -> 确认；有序缺口等待；无序逐包去重；终态互斥。"""
from __future__ import annotations

import threading
import pytest

from app import paths
from app.engine import (
    DEFAULT_ACK,
    PKT_ACKED,
    PKT_DELIVERED,
    PKT_IN_FLIGHT,
    PKT_TIMED_OUT,
)
from app.errors import (
    ERR_NOT_TIMEOUT,
    ERR_PACKET_FINALIZED,
    ERR_PROOF_VALUE,
    ERR_SEQ_GAP,
    ERR_TIMEOUT,
)
from app.relayer import Relayer

BLOCK = 10 * 1_000_000_000


def _send(net: Relayer, ordering: str = "UNORDERED", **over):
    net.handshake("chainA", "chainB", ordering=ordering, version="ics20-1")
    kw = dict(
        data_hex=b"hello-ibc".hex(),
        timeout_height=100,
        timeout_time_nanos=0,
        amount=1000,
        sender="alice",
        receiver="bob",
    )
    kw.update(over)
    return net.send_and_commit("chainA", "channel-0", **kw)


# ---------------------------------------------------------------- 正常路径

def test_unordered_happy_path_deliver_and_ack(network: Relayer):
    net = network
    pkt = _send(net)
    assert pkt["status"] == PKT_IN_FLIGHT
    assert pkt["sequence"] == 1

    r = net.relay_recv(pkt)
    assert r["result"] == "received"
    assert bytes.fromhex(r["ack"]) == DEFAULT_ACK

    a = net.relay_ack(pkt)
    assert a["final_status"] == PKT_ACKED

    info = net.engine.packet_info("chainA", "channel-0", 1)
    assert info["status"] == PKT_ACKED
    # 承诺在确认后已删除：非成员证明成立
    p = net.proof("chainA", paths.packet_commitment_key("channel-0", 1))
    assert p["membership"] is False


def test_unordered_packets_deliver_out_of_order_and_dedupe(network: Relayer):
    net = network
    p1 = _send(net, timeout_height=100)
    p2 = net.send_and_commit("chainA", "channel-0",
                             data_hex=b"two".hex(), timeout_height=100, timeout_time_nanos=0)
    p3 = net.send_and_commit("chainA", "channel-0",
                             data_hex=b"three".hex(), timeout_height=100, timeout_time_nanos=0)
    # 故意乱序：3 -> 1 -> 2，全部成功
    assert net.relay_recv(p3)["result"] == "received"
    assert net.relay_recv(p1)["result"] == "received"
    assert net.relay_recv(p2)["result"] == "received"
    # 逐包去重：重复投递不写第二遍，也不报错
    dup = net.relay_recv(p2)
    assert dup["result"] == "already_received" and dup["duplicate"] is True

    # 每个包独立确认（乱序也可以）
    assert net.relay_ack(p3)["final_status"] == PKT_ACKED
    assert net.relay_ack(p1)["final_status"] == PKT_ACKED
    assert net.relay_ack(p2)["final_status"] == PKT_ACKED


def test_ordered_must_wait_on_gap_then_resume(network: Relayer):
    net = network
    p1 = _send(net, ordering="ORDERED", timeout_height=100)
    p2 = net.send_and_commit("chainA", "channel-0",
                             data_hex=b"two".hex(), timeout_height=100, timeout_time_nanos=0)
    # 先到 2 号：有序通道必须等待，返回 SEQUENCE_GAP，不写任何状态
    proof2 = net.proof("chainA", paths.packet_commitment_key("channel-0", 2))
    with pytest.raises(Exception) as ei:
        net.engine.recv_packet(p2, proof2)
    assert ei.value.code == ERR_SEQ_GAP
    b = net.engine.channel_info("chainB", "channel-0")
    assert b["next_seq_recv"] == 1  # 缺口未消费，序列号没动

    # 收到 1 号后，2 号即可处理
    assert net.relay_recv(p1)["result"] == "received"
    assert net.relay_recv(p2)["result"] == "received"
    assert net.engine.channel_info("chainB", "channel-0")["next_seq_recv"] == 3


def test_ordered_ack_requires_next_seq_recv_coverage(network: Relayer):
    net = network
    p1 = _send(net, ordering="ORDERED", timeout_height=100)
    net.relay_recv(p1)
    # 此时目的链已提交；构造一个截断的 nsn 证明（用空值证明伪装未覆盖）应失败
    proofs = {
        "proof_ack": net.proof("chainB", paths.packet_ack_key("channel-0", 1)),
        "proof_next_seq_recv": net.proof("chainB", paths.next_seq_recv_key("channel-0")),
    }
    assert net.engine.acknowledge_packet(p1, DEFAULT_ACK.hex(), proofs)["final_status"] == PKT_ACKED


# ---------------------------------------------------------------- 互斥/重放

def test_duplicate_ack_is_rejected_after_final_state(network: Relayer):
    net = network
    pkt = _send(net)
    net.relay_recv(pkt)
    net.relay_ack(pkt)
    with pytest.raises(Exception) as ei:
        net.relay_ack(pkt)
    assert ei.value.code == ERR_PACKET_FINALIZED


def test_ack_with_wrong_content_rejected(network: Relayer):
    """证明锚定的是目的链真实写入的 ack 承诺；源链声称别的 ack 内容必须被拒。"""
    net = network
    pkt = _send(net)
    net.relay_recv(pkt, ack_hex=b"success".hex())
    proofs = {
        "proof_ack": net.proof("chainB", paths.packet_ack_key("channel-0", 1)),
        "proof_receipt": net.proof("chainB", paths.packet_receipt_key("channel-0", 1)),
        "proof_next_seq_recv": net.proof("chainB", paths.next_seq_recv_key("channel-0")),
    }
    with pytest.raises(Exception) as ei:
        net.engine.acknowledge_packet(pkt, b"forged-different-ack".hex(), proofs)
    assert ei.value.code == ERR_PROOF_VALUE
    # 包仍未落终态，可用正确的 ack 完成
    assert net.engine.acknowledge_packet(
        pkt, b"success".hex(), proofs
    )["final_status"] == PKT_ACKED


def test_ack_after_timeout_refused_and_only_one_terminal_wins(network: Relayer):
    """协议层互斥：超时成功后，即使之后目的链把包收了（异常场景），
    源链也必须拒绝确认——终态只能落一种。"""
    net = network
    pkt = _send(net, timeout_height=3, timeout_time_nanos=0)
    # 推进目的链到超时边界之后，源链执行超时退款
    net.commit("chainB")  # height 1
    net.commit("chainB")  # height 2
    net.commit("chainB")  # height 3 >= timeout
    tp = net.timeout_proofs(pkt)
    tout = net.engine.timeout_packet(pkt, tp)
    assert tout["final_status"] == PKT_TIMED_OUT
    assert tout["refunded_amount"] == 1000

    # 托管池已退款：alice 先支出 1000 再收回 1000，净额为 0
    accts = {a["account"]: a["amount"] for a in net.engine.escrow_accounts("chainA", "channel-0")}
    assert accts["alice"] == 0 and accts["__escrow__"] == 0

    # 再确认必须被终态互斥拒绝
    with pytest.raises(Exception) as ei:
        net.relay_ack(pkt)
    assert ei.value.code in (ERR_PACKET_FINALIZED, ERR_PROOF_VALUE)


def test_concurrent_duplicate_ack_only_one_wins(network: Relayer):
    """多线程并发提交同一笔确认（真实 HTTP 无关，直接打引擎事务）：
    SQLite 串行化 + 条件 UPDATE，恰好一个成功。"""
    net = network
    pkt = _send(net)
    net.relay_recv(pkt)
    proofs = {
        "proof_ack": net.proof("chainB", paths.packet_ack_key("channel-0", 1)),
        "proof_receipt": net.proof("chainB", paths.packet_receipt_key("channel-0", 1)),
        "proof_next_seq_recv": net.proof("chainB", paths.next_seq_recv_key("channel-0")),
    }

    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            net.engine.acknowledge_packet(pkt, DEFAULT_ACK.hex(), proofs)
            with lock:
                results.append("ok")
        except Exception as e:  # noqa: BLE001
            with lock:
                results.append(getattr(e, "code", str(e)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1, results
    assert all(r in ("ok", ERR_PACKET_FINALIZED) for r in results)
    assert net.engine.packet_info("chainA", "channel-0", 1)["status"] == PKT_ACKED


def test_concurrent_ack_and_timeout_only_one_terminal(network: Relayer):
    """并发确认与超时：两种终态事务竞争，只能落一个。

    构造：包在目的链 height 1 被正常接收（未超时），随后目的链推进到超时阈值。
    此时
    - 确认使用“已收到”检查点上的 receipt/ack 证明（检查点高度 1，仍在信任期）；
    - 超时使用当前“已有回执”状态的非成员证明，协议证明层必须先拒绝超时；
    再单独用一笔证明合法但终态竞争的并发确认（双 ack）压测条件 UPDATE 互斥，
    最终状态唯一。"""
    net = network
    pkt = _send(net, timeout_height=3, timeout_time_nanos=0)
    net.relay_recv(pkt)  # 目的链 height 1：收到并提交
    net.commit("chainB")  # height 2
    net.commit("chainB")  # height 3：达到超时阈值

    ack_proofs = {
        "proof_ack": net.proof("chainB", paths.packet_ack_key("channel-0", 1)),
        "proof_receipt": net.proof("chainB", paths.packet_receipt_key("channel-0", 1)),
        "proof_next_seq_recv": net.proof("chainB", paths.next_seq_recv_key("channel-0")),
    }
    # 超时证明：无序通道要求回执不存在 —— 回执实际存在，证明层应拒绝
    tout_proofs = {
        "proof_unreceived": net.proof("chainB", paths.packet_receipt_key("channel-0", 1)),
    }
    with pytest.raises(Exception) as ei:
        net.engine.timeout_packet(pkt, tout_proofs)
    assert ei.value.code == ERR_PROOF_VALUE

    # 同时并发两笔相同确认，依旧恰好一个终态
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def do_ack():
        barrier.wait()
        try:
            net.engine.acknowledge_packet(pkt, DEFAULT_ACK.hex(), ack_proofs)
            with lock:
                outcomes.append("ACKED")
        except Exception as e:  # noqa: BLE001
            with lock:
                outcomes.append("rej:" + getattr(e, "code", str(e)))

    threads = [threading.Thread(target=do_ack) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ACKED") == 1, outcomes
    assert net.engine.packet_info("chainA", "channel-0", 1)["status"] == PKT_ACKED
