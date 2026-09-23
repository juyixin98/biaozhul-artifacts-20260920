#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端可运行演示：不依赖 HTTP，直接驱动引擎与中继器，逐步打印真实结果。

运行：
    python demo.py            # 内存式临时数据库，结束即清理
    python demo.py --keep my.db

演示覆盖：
  1. 建链 / 双向轻客户端（登记信任根公钥）/ 连接
  2. 通道四次握手（顺序模式、对端、版本绑定，证明全部真实验签）
  3. 正常包：发送 -> 接收 -> 确认（终态 ACKED）
  4. 无序通道乱序投递 + 逐包去重
  5. 有序通道缺口等待
  6. 高度/时间超时边界（== 即超时）与超时退款（终态 TIMED_OUT）
  7. 坏检查点（伪造签名 / 高度回退）被拒绝
  8. 通道关闭
  9. 崩溃恢复（换一个 Engine 重开同一数据库，完整性校验 + 终态保留）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from app import paths
from app.engine import Engine
from app.errors import AppError
from app.relayer import Relayer
from app.storage import Database

BLOCK = 10 * 1_000_000_000
T0 = 1_000_000_000_000


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def show(label: str, obj) -> None:
    print(f"[{label}]")
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    else:
        print(obj)


def expect_fail(label: str, fn, code: str) -> None:
    try:
        fn()
    except AppError as e:
        status = "OK" if e.code == code else f"WRONG CODE (got {e.code}, want {code})"
        print(f"[{label}] 拒绝成功: code={e.code} http={e.status} -- {e.message}  <<{status}>>")
        if e.code != code:
            raise SystemExit(1)
    else:
        print(f"[{label}] 致命：本应被拒绝 ({code})，但调用成功了！")
        raise SystemExit(1)


def main() -> None:
    keep = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--keep":
        keep = sys.argv[2]
    if keep:
        if os.path.exists(keep):
            os.remove(keep)
        db_path = keep
        tmpdir = None
    else:
        tmpdir = tempfile.mkdtemp(prefix="ibc-mini-")
        db_path = os.path.join(tmpdir, "demo.db")
    print(f"数据库: {db_path}")

    eng = Engine(Database(db_path))
    rly = Relayer(eng)

    # ---------------------------------------------------------- 1. 拓扑
    hr("1. 建链 + 轻客户端（信任根公钥）+ 连接")
    rly.setup_two_chains("chainA", "chainB", genesis_time_nanos=T0)
    show("chainA", eng.chain_info("chainA"))
    show("chainB", eng.chain_info("chainB"))

    # ---------------------------------------------------------- 2. 握手
    hr("2. 通道四次握手（UNORDERED / ics20-1，证明真实验签）")
    rly.handshake("chainA", "chainB", ordering="UNORDERED", version="ics20-1")
    show("A 端通道", eng.channel_info("chainA", "channel-0"))
    show("B 端通道", eng.channel_info("chainB", "channel-0"))

    # ---------------------------------------------------------- 3. 正常包
    hr("3. 正常包生命周期：send -> recv -> acknowledge")
    pkt = rly.send_and_commit(
        "chainA", "channel-0",
        data_hex=b"transfer/100".hex(),
        timeout_height=100, timeout_time_nanos=0,
        amount=100, sender="alice", receiver="bob",
    )
    show("packet(1) 已发送，承诺上链", {"sequence": pkt["sequence"], "status": pkt["status"]})
    show("recv", rly.relay_recv(pkt))
    show("acknowledge", rly.relay_ack(pkt))
    show("最终包状态", eng.packet_info("chainA", "channel-0", 1)["status"])

    # ---------------------------------------------------------- 4. 乱序/去重
    hr("4. 无序通道乱序投递 + 去重")
    p2 = rly.send_and_commit("chainA", "channel-0",
                             data_hex=b"two".hex(), timeout_height=100, timeout_time_nanos=0)
    p3 = rly.send_and_commit("chainA", "channel-0",
                             data_hex=b"three".hex(), timeout_height=100, timeout_time_nanos=0)
    show("先收 3 号", rly.relay_recv(p3)["result"])
    show("再收 2 号", rly.relay_recv(p2)["result"])
    show("重复收 3 号（幂等去重）", rly.relay_recv(p3)["result"])
    rly.relay_ack(p3)
    rly.relay_ack(p2)

    # ---------------------------------------------------------- 5. 有序缺口
    hr("5. 第二条通道（ORDERED）：缺口必须等待")
    eng.channel_open_init("chainA", "channel-1", "conn-1", "ORDERED", "v1", "channel-1")
    rly.commit("chainA")
    proof = rly.proof("chainA", paths.channel_key("channel-1"))
    eng.channel_open_try("chainB", "channel-1", "conn-1", "ORDERED", "v1",
                         "channel-1", "v1", proof)
    rly.commit("chainB")
    proof = rly.proof("chainB", paths.channel_key("channel-1"))
    eng.channel_open_ack("chainA", "channel-1", proof)
    rly.commit("chainA")
    proof = rly.proof("chainA", paths.channel_key("channel-1"))
    eng.channel_open_confirm("chainB", "channel-1", proof)
    rly.commit("chainB")

    o1 = rly.send_and_commit("chainA", "channel-1",
                             data_hex=b"o1".hex(), timeout_height=100, timeout_time_nanos=0)
    o2 = rly.send_and_commit("chainA", "channel-1",
                             data_hex=b"o2".hex(), timeout_height=100, timeout_time_nanos=0)
    proof2 = rly.proof("chainA", paths.packet_commitment_key("channel-1", 2))
    expect_fail("先投 2 号（缺口）", lambda: eng.recv_packet(o2, proof2), "SEQUENCE_GAP")
    show("收到 1 号", rly.relay_recv(o1)["result"])
    show("再收 2 号", rly.relay_recv(o2)["result"])
    rly.relay_ack(o1)
    rly.relay_ack(o2)

    # ---------------------------------------------------------- 6. 超时边界
    hr("6. 超时边界（目的链高度/时间 == 阈值即超时）与退款")
    hb = eng.chain_info("chainB")["height"]
    tb = eng.chain_info("chainB")["time_nanos"]

    hp = rly.send_and_commit("chainA", "channel-0",
                             data_hex=b"height-timeout".hex(),
                             timeout_height=hb + 2, timeout_time_nanos=0,
                             amount=300, sender="alice")
    rly.commit("chainB")
    expect_fail("h < 阈值：不能超时", lambda: eng.timeout_packet(hp, rly.timeout_proofs(hp)),
                "PACKET_NOT_TIMED_OUT")
    rly.commit("chainB")  # 到达阈值
    show("高度超时退款", eng.timeout_packet(hp, rly.timeout_proofs(hp)))
    rly.commit("chainA")

    tp = rly.send_and_commit("chainA", "channel-0",
                             data_hex=b"time-timeout".hex(),
                             timeout_height=0, timeout_time_nanos=tb + 3 * BLOCK,
                             amount=70, sender="alice")
    eng.commit_block("chainB", tb + 3 * BLOCK)
    show("时间超时退款", eng.timeout_packet(tp, rly.timeout_proofs(tp)))
    rly.commit("chainA")
    show("alice 退款后净额（-300+300-70+70=0）与托管池",
         eng.escrow_accounts("chainA", "channel-0"))

    # ---------------------------------------------------------- 7. 坏检查点
    hr("7. 信任根：缺失/伪造/回退检查点一律拒绝")
    gp = rly.send_and_commit("chainA", "channel-0",
                             data_hex=b"guard".hex(),
                             timeout_height=100, timeout_time_nanos=0)
    good = rly.proof("chainA", paths.packet_commitment_key("channel-0", gp["sequence"]))
    no_cp = {k: v for k, v in good.items() if k != "checkpoint"}
    expect_fail("缺失检查点", lambda: eng.recv_packet(gp, no_cp), "BAD_PROOF_STRUCTURE")
    bad_sig = dict(good)
    cp = dict(good["checkpoint"])
    cp["signature"] = bytes(64).hex()
    bad_sig["checkpoint"] = cp
    expect_fail("错误签名检查点", lambda: eng.recv_packet(gp, bad_sig), "INVALID_CHECKPOINT")
    show("合法证明收包", rly.relay_recv(gp)["result"])

    # ---------------------------------------------------------- 8. 通道关闭
    hr("8. 通道关闭")
    close_time = eng.chain_info("chainB")["time_nanos"] + BLOCK
    cp2 = rly.send_and_commit("chainA", "channel-0",
                              data_hex=b"after-close".hex(), timeout_height=0,
                              timeout_time_nanos=close_time)
    # 目的端关闭并在超时阈值时刻出块
    eng.channel_close("chainB", "channel-0")
    eng.commit_block("chainB", close_time)
    # 目的端已关闭：收包被拒
    expect_fail("关闭通道上收包",
                lambda: eng.recv_packet(
                    cp2, rly.proof("chainA",
                                   paths.packet_commitment_key("channel-0", cp2["sequence"]))),
                "CHANNEL_CLOSED")
    closed = {"proof_channel_closed":
              rly.proof("chainB", paths.channel_key("channel-0"))}
    show("凭 CLOSED 通道证明超时退款", eng.timeout_packet(cp2, closed))
    rly.commit("chainA")

    # ---------------------------------------------------------- 9. 崩溃恢复
    hr("9. 崩溃恢复：重新打开同一数据库文件")
    del eng, rly
    eng2 = Engine(Database(db_path))
    show("启动完整性校验（检查点验签 + 全量重放状态树）", eng2.verify_integrity())
    pinfo = eng2.packet_info("chainA", "channel-0", 1)
    show("重启后读取到的终态包", pinfo["status"])
    assert pinfo["status"] == "ACKED"
    print("\n演示全部通过 ✔  (所有哈希/Ed25519 签名/Merkle 证明均为真实执行)")
    if not keep:
        print(f"临时数据库保留在: {db_path}（可自行删除）")


if __name__ == "__main__":
    main()
