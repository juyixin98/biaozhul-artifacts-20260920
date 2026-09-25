#!/usr/bin/env python3
"""端到端演示（纯命令行，无前端）：在临时目录起日志服务，走完核心流程。

流程
----
1. 追加若干叶子（构造非二次幂 13 叶）。
2. 取 STH 并验证 Ed25519 签名；记录「旧 STH」。
3. 对每一片叶子取包含证明，客户端本地重算根核验（逐项）。
4. 主动演示攻击：伪造旧根 / 错误索引 / 篡改路径，验证器全部拒绝。
5. 继续追加到 20 叶，用旧 STH 的根 + 一致性证明连接到新 STH。
6. 演示边界：同一树大小的一致性、历史树大小上的包含证明。

直接运行：``python3 scripts/demo.py``
"""

from __future__ import annotations

import os
import sys
import tempfile

# 允许直接 `python3 scripts/demo.py` 从项目根运行（把项目根加入 sys.path）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tlog.log import Log
from tlog.merkle import (
    verify_consistency,
    verify_inclusion,
)


def step(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tlog-demo-")
    print(f"数据目录（临时）：{tmp}")
    log = Log(tmp)

    # 1. 追加 13 条叶子（13 是非二次幂） ---------------------------------
    step("1. 追加 13 条叶子（非二次幂）")
    leaves = [f"事件记录 {i:02d}".encode() for i in range(13)]
    for i, data in enumerate(leaves):
        idx = log.append(data, added_ms=1_700_000_000_000 + i)
        assert idx == i
    old_root = log.root()
    print(f"tree_size = {log.size}")
    print(f"SHA256 树根 = {old_root.hex()}")

    # 2. 取 STH 并验签 ----------------------------------------------------
    step("2. 获取签名树头 STH 并验证 Ed25519 签名")
    sth = log.get_sth(timestamp_ms=1_700_000_001_000)
    print(f"tree_size={sth['tree_size']}  timestamp_ms={sth['timestamp_ms']}")
    print(f"root={sth['sha256_root_hash']}")
    print(f"公钥(hex)={sth['public_key_hex']}")
    print(f"签名(hex)={sth['tree_head_signature'][:32]}…")
    print("验签：", "通过 ✓" if log.verify_sth(sth) else "失败 ✗")

    # 3. 小树逐项核验 -----------------------------------------------------
    step("3. 对全部 13 片叶子逐项生成并验证包含证明")
    all_ok = True
    for m in range(13):
        lh, proof, root, n = log.inclusion_proof(m)
        ok = verify_inclusion(lh, m, n, proof, root)
        all_ok &= ok
        print(
            f"  叶子 {m:2d}: 路径长度={len(proof)}  "
            f"首元素={proof[0].hex()[:16] if proof else '(空)'}…  验证={'✓' if ok else '✗'}"
        )
    print(f"13/13 全部通过：{all_ok}")

    # 4. 攻击演示 ---------------------------------------------------------
    step("4. 攻击演示（验证器必须全部拒绝）")

    lh3, proof3, root3, _ = log.inclusion_proof(3)

    forged_root = bytes(root3[i] ^ 0x01 for i in range(32))
    print(
        "4a. 伪造旧根（首字节翻转）验证包含证明：",
        "拒绝 ✓"
        if not verify_inclusion(lh3, 3, 13, proof3, forged_root)
        else "意外通过 ✗",
    )

    wrong_index_ok = True
    for bad_m in (0, 2, 4, 12):
        if verify_inclusion(lh3, bad_m, 13, proof3, root3):
            wrong_index_ok = False
    print("4b. 用错误索引 0/2/4/12 验证同一份证明：", "全部拒绝 ✓" if wrong_index_ok else "有通过 ✗")

    tampered = list(proof3)
    tampered[0] = bytes(tampered[0][i] ^ 0xFF for i in range(32))
    print(
        "4c. 篡改路径首元素：",
        "拒绝 ✓" if not verify_inclusion(lh3, 3, 13, tampered, root3) else "意外通过 ✗"
    )

    print(
        "4d. 路径截断（少一个兄弟）：",
        "拒绝 ✓" if not verify_inclusion(lh3, 3, 13, proof3[:-1], root3) else "意外通过 ✗"
    )

    # 篡改 STH 任一字段后验签
    forged_sth = dict(sth)
    forged_sth["tree_size"] = 14
    print(
        "4e. STH 树大小被改成 14 后验签：",
        "拒绝 ✓" if not log.verify_sth(forged_sth) else "意外通过 ✗"
    )

    # 5. 继续追加 + 一致性证明 --------------------------------------------
    step("5. 追加到 20 叶，用一致性证明把旧 STH 连到新 STH")
    for i in range(13, 20):
        log.append(f"事件记录 {i:02d}".encode(), added_ms=1_700_000_002_000 + i)
    new_sth = log.get_sth(timestamp_ms=1_700_000_003_000)
    new_root = bytes.fromhex(new_sth["sha256_root_hash"])
    print(f"旧 STH: size=13 root={old_root.hex()[:24]}…")
    print(f"新 STH: size=20 root={new_root.hex()[:24]}…")

    for first in (1, 4, 7, 13):
        old, new, proof, fn, sn = log.consistency_proof(first)
        ok = verify_consistency(fn, sn, proof, old, new)
        print(f"  一致性 PROOF(first={first:2d}, n=20): 路径长度={len(proof)}  验证={'✓' if ok else '✗'}")

    old2, new2, proof13, fn, sn = log.consistency_proof(13)
    ok = verify_consistency(fn, sn, proof13, old2, new2)
    assert ok and old2 == old_root and new2 == new_root
    print("→ 旧 STH(13 叶) 与新 STH(20 叶) 前缀一致性：通过 ✓")
    print("  含义：20 叶树保留了旧 13 叶的全部内容与顺序，仅在末尾追加。")

    # 6. 历史树大小上的包含证明 -------------------------------------------
    step("6. 在「旧大小 13」上验证历史包含证明（观察者拿着旧 STH 也能验）")
    lh0, proof0, root0, n0 = log.inclusion_proof(0, tree_size=13)
    ok = verify_inclusion(lh0, 0, 13, proof0, old_root)
    print(f"  在 size=13 的旧根上验证叶子 0：{'通过 ✓' if ok else '失败 ✗'}")

    step("7. 明确不解决的问题：日志分叉（forking / equivocation）")
    print(
        "本项目只证明：同一个观察者先后看到的 STH 之间前缀一致。\n"
        "运营者若用另一套叶子独立维护第二棵树、向不互通的观察者出示\n"
        "不同 STH（分叉共谋），一致性证明无法跨「两条独立视图」发现。\n"
        "这需要外部机制（如 gossip / witness cosigning / STH 审计），\n"
        "本项目按需求明确不实现，也不声称防御。"
    )

    print()
    print("演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
