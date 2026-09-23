#!/usr/bin/env python3
"""端到端演示：真实 Ed25519 签名、真 PostgreSQL、真实对账与落盘。

直接走服务层（无需启动 uvicorn）。每个场景使用独立链 ID，互不干扰。
运行：
    export DATABASE_URL="postgresql+psycopg2://inbox:inbox@localhost:55432/inbox"
    python -m scripts.demo
加 --reset 可清空并重建全部表。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from app import chains as chains_svc
from app import db as db_mod
from app import reconcile as recon_svc
from app.demo_crypto import DemoFeeder
from app.ingest import ingest_event
from app.models import ChainEvent
from tests.factories import ZERO, block_hash, fork_hash, tx_hash  # 复用确定性构造

BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def event(feeder, *, chain_id, kind, nonce, source_chain, contract, amount,
          height, token_id="", log_index=0, tx_suffix="", block_hash_override=None):
    payload = {
        "chain_id": chain_id,
        "event_type": kind,
        "tx_hash": tx_hash(chain_id, nonce + tx_suffix),
        "log_index": log_index,
        "block_height": height,
        "block_hash": block_hash_override or block_hash(chain_id, height),
        "source_chain": source_chain,
        "original_contract": contract,
        "token_id": token_id,
        "source_nonce": nonce,
        "account": "0xReceiver" if kind in ("MINT", "RELEASE") else "0xLocker",
        "amount": str(amount),
    }
    from app.crypto import sign_payload
    return {
        "payload": payload,
        "signature_hex": sign_payload(feeder.private_key, payload),
        "feeder_public_key_hex": feeder.public_key,
    }


def feed(db, env) -> None:
    res = ingest_event(db, env["payload"], env["signature_hex"], env["feeder_public_key_hex"])
    p = env["payload"]
    print(f"  ingest {p['event_type']:7s} chain={p['chain_id']:9s} "
          f"h={p['block_height']} amount={p['amount']:>4s} nonce={p['source_nonce']:8s} "
          f"-> {res['status']}")
    db.commit()


def chain(db, cid, feeder, *, depth=2, timeout=3600, heights=range(0, 6), tip=None, times=None):
    chains_svc.register_chain(db, cid, cid, depth, timeout)
    chains_svc.register_feeder(db, cid, feeder.public_key, feeder.label)
    for h in heights:
        bt = (times or {}).get(h) or (BASE + timedelta(seconds=12 * h))
        chains_svc.upsert_block(
            db, cid, h, block_hash(cid, h),
            ZERO if h == 0 else block_hash(cid, h - 1), bt,
        )
    chains_svc.set_tip(db, cid, block_hash(cid, tip if tip is not None else max(heights)))
    db.commit()


def run(db, label, as_of=None, chain_prefix=("demo",)):
    res = recon_svc.run_reconciliation(db, as_of=as_of or BASE + timedelta(seconds=600))
    db.commit()
    if isinstance(chain_prefix, str):
        chain_prefix = (chain_prefix,)
    # 对账器是全库视角；演示按证据文件中出现的链 ID 前缀过滤展示
    # （完整快照/证据仍含全库内容，不做任何裁剪）。
    print(f"\n▶ 对账[{label}] run={res['run_id']}")
    print(f"  snapshot : {res['snapshot_path']}")
    print(f"  hash     : {res['snapshot_hash'][:16]}…  prev={res['prev_snapshot_hash'][:16]}…")
    scoped = []
    for f in res["findings"]:
        try:
            doc = json.load(open(f["evidence_path"]))
        except OSError:
            continue
        ev = doc.get("evidence", {})
        chains = {ev.get("chain_id")}
        for key in ("lock", "mint", "burn", "release", "event"):
            if isinstance(ev.get(key), dict):
                chains.add(ev[key].get("chain_id"))
        for e in ev.get("confirmed_events", []) + ev.get("all_mints", []) + ev.get("events", []):
            if isinstance(e, dict):
                chains.add(e.get("chain_id"))
        chains.discard(None)
        if any(isinstance(c, str) and c.startswith(chain_prefix) for c in chains):
            scoped.append(f)
    if scoped:
        print(f"  findings ({len(scoped)}):")
        for f in scoped:
            print(f"    - [{f['severity']}] {f['code']}: {f['detail']}")
            print(f"        evidence: {f['evidence_path']}")
    else:
        print("  findings : 无（本场景全部守恒配对）")
    return res


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="清空并重建所有表")
    args = parser.parse_args()

    db_mod.init_engine()
    if args.reset:
        db_mod.drop_all()
    db_mod.create_all()
    db = db_mod.db_session()

    fA = DemoFeeder("feeder-eth", seed=b"demo-seed-ethereum-chain-A-key!!")
    fB = DemoFeeder("feeder-pol", seed=b"demo-seed-polygon-chain-B-key!!!")

    # 场景1：正常 LOCK->MINT + BURN->RELEASE，守恒成立 ----------------------
    hr("场景1：正常往返（LOCK↔MINT、BURN↔RELEASE），金额守恒")
    chain(db, "demo-eth", fA)
    chain(db, "demo-pol", fB)
    feed(db, event(fA, chain_id="demo-eth", kind="LOCK", nonce="n1",
                   source_chain="demo-eth", contract="0xTokenA", amount=100, height=1))
    feed(db, event(fB, chain_id="demo-pol", kind="MINT", nonce="n1",
                   source_chain="demo-eth", contract="0xTokenA", amount=100, height=1))
    feed(db, event(fB, chain_id="demo-pol", kind="BURN", nonce="n2",
                   source_chain="demo-eth", contract="0xTokenA", amount=40, height=2))
    feed(db, event(fA, chain_id="demo-eth", kind="RELEASE", nonce="n2",
                   source_chain="demo-eth", contract="0xTokenA", amount=40, height=2))
    run(db, "正常", chain_prefix=("demo-eth", "demo-pol"))

    # 场景2：乱序（MINT 先到，LOCK 后到） -----------------------------------
    hr("场景2：乱序（MINT 先到，LOCK 后到）")
    mint = event(fB, chain_id="demo-pol", kind="MINT", nonce="n3",
                 source_chain="demo-eth", contract="0xTokenB", amount=25, height=3)
    feed(db, mint)
    run(db, "乱序-仅MINT", chain_prefix=("demo-eth", "demo-pol"))
    feed(db, event(fA, chain_id="demo-eth", kind="LOCK", nonce="n3",
                   source_chain="demo-eth", contract="0xTokenB", amount=25, height=3))
    run(db, "乱序-LOCK补达", chain_prefix=("demo-eth", "demo-pol"))

    # 场景3：超时未配对 LOCK -------------------------------------------------
    hr("场景3：超时未配对 LOCK（timeout=60s，as_of 在 1 天后）")
    chains_svc.register_chain(db, "demo-eth2", "eth2", 1, 60)
    chains_svc.register_feeder(db, "demo-eth2", fA.public_key, fA.label)
    for h in range(0, 4):
        chains_svc.upsert_block(db, "demo-eth2", h, block_hash("demo-eth2", h),
                                ZERO if h == 0 else block_hash("demo-eth2", h - 1), BASE)
    chains_svc.set_tip(db, "demo-eth2", block_hash("demo-eth2", 3))
    db.commit()
    feed(db, event(fA, chain_id="demo-eth2", kind="LOCK", nonce="n4",
                   source_chain="demo-eth2", contract="0xSlow", amount=7, height=1))
    run(db, "超时", as_of=BASE + timedelta(days=1), chain_prefix=("demo-eth2",))

    # 场景4：分叉撤销 --------------------------------------------------------
    hr("场景4：分叉撤销（LOCK 先确认，随后被竞争链甩出）")
    chain(db, "demo-eth3", fA, heights=[0, 1, 2, 3, 4])
    chain(db, "demo-pol3", fB, heights=[0, 1, 2, 3, 4])
    lock_env = event(fA, chain_id="demo-eth3", kind="LOCK", nonce="n5",
                     source_chain="demo-eth3", contract="0xReorg", amount=9, height=1)
    feed(db, lock_env)
    feed(db, event(fB, chain_id="demo-pol3", kind="MINT", nonce="n5",
                   source_chain="demo-eth3", contract="0xReorg", amount=9, height=1))
    run(db, "分叉前", chain_prefix=("demo-eth3", "demo-pol3"))
    prev = block_hash("demo-eth3", 0)
    for h in range(1, 6):
        fh = fork_hash("demo-eth3", h, "demo")
        chains_svc.upsert_block(db, "demo-eth3", h, fh, prev,
                                BASE + timedelta(seconds=100 + h))
        prev = fh
    chains_svc.set_tip(db, "demo-eth3", prev)
    db.commit()
    run(db, "分叉后", chain_prefix=("demo-eth3", "demo-pol3"))

    # 场景5：两链相同合约地址 + 重复铸造 + 无锁定铸造 -----------------------
    hr("场景5：两链相同地址不碰撞；重复铸造；无锁定铸造")
    chain(db, "demo-eth4", fA, heights=range(0, 8))
    chain(db, "demo-pol4", fB, heights=range(0, 8))
    same = "0xSameAddressOnBothChains"
    feed(db, event(fA, chain_id="demo-eth4", kind="LOCK", nonce="n6",
                   source_chain="demo-eth4", contract=same, amount=10, height=1))
    feed(db, event(fB, chain_id="demo-pol4", kind="MINT", nonce="n6",
                   source_chain="demo-eth4", contract=same, amount=10, height=1))
    feed(db, event(fB, chain_id="demo-pol4", kind="MINT", nonce="n6",
                   source_chain="demo-eth4", contract=same, amount=10,
                   height=2, tx_suffix="#dup"))
    feed(db, event(fB, chain_id="demo-pol4", kind="MINT", nonce="n7",
                   source_chain="demo-eth4", contract=same, amount=10, height=3))
    # 原生资产在 demo-pol4 上、用同一地址（UID 必须不同）
    feed(db, event(fB, chain_id="demo-pol4", kind="LOCK", nonce="n8",
                   source_chain="demo-pol4", contract=same, amount=3, height=4))
    feed(db, event(fA, chain_id="demo-eth4", kind="MINT", nonce="n8",
                   source_chain="demo-pol4", contract=same, amount=3, height=4))
    # 重复投递（幂等）
    dup = event(fA, chain_id="demo-eth4", kind="LOCK", nonce="n9",
                source_chain="demo-eth4", contract="0xIdem", amount=5, height=2)
    feed(db, dup)
    feed(db, dup)
    res = run(db, "综合违例", chain_prefix=("demo-eth4", "demo-pol4"))

    # 快照内事件状态统计
    snap = json.load(open(res["snapshot_path"]))
    print("\n事件状态（全库）：", {
        s: sum(1 for e in snap["events"] if e["status"] == s)
        for s in ("CONFIRMED", "PENDING", "REORGED")
    })
    print("投递统计：", snap["delivery"])
    print("\n演示完成。所有快照见 data/snapshots/，证据见 data/evidence/<run-id>/。")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
