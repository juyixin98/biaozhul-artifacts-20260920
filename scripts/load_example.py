#!/usr/bin/env python3
"""加载 examples/events.json：登记链/公钥/块头 → 真实验签投递 → 离线对账。

这是一份真实可执行的示例输入消费程序，证明 examples/events.json 里的签名、
资产 UID、消息配对都由真实密码学/协议逻辑处理，而非占位数据。

运行：
    export DATABASE_URL="postgresql+psycopg2://inbox:inbox@localhost:55432/inbox"
    python -m scripts.load_example            # 使用 examples/events.json
    python -m scripts.load_example --reset    # 先清库重建
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from app import chains as chains_svc
from app import db as db_mod
from app import reconcile as recon_svc
from app.ingest import IngestError, ingest_event

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "examples" / "events.json"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=str(EXAMPLE_PATH))
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    doc = json.loads(Path(args.file).read_text(encoding="utf-8"))

    db_mod.init_engine()
    if args.reset:
        db_mod.drop_all()
    db_mod.create_all()
    db = db_mod.db_session()

    # 1) 登记链与 feeder 公钥
    for c in doc["chains"]:
        chains_svc.register_chain(
            db, c["chain_id"], c.get("name", ""),
            int(c["confirmation_depth"]), int(c["message_timeout_seconds"]),
        )
        chains_svc.register_feeder(
            db, c["chain_id"], c["feeder_public_key_hex"], c.get("name", "")
        )
    db.commit()

    # 2) 登记块头并设置 tip
    for chain_id, b in doc["blocks"].items():
        for blk in b["blocks"]:
            chains_svc.upsert_block(
                db, chain_id, blk["block_height"], blk["block_hash"],
                blk["parent_hash"], _dt(blk["block_time"]),
            )
        if b.get("tip_block_hash"):
            chains_svc.set_tip(db, chain_id, b["tip_block_hash"])
    db.commit()

    # 3) 逐条真实验签投递（重复也只计数一次）
    accepted = duplicated = rejected = 0
    for env in doc["events_batch"]:
        try:
            res = ingest_event(
                db, env["payload"], env["signature_hex"], env["feeder_public_key_hex"]
            )
            if res["status"] == "accepted":
                accepted += 1
            else:
                duplicated += 1
        except IngestError as exc:
            rejected += 1
            p = env["payload"]
            print(f"  REJECTED {p.get('event_type')} {p.get('source_nonce')}: {exc}")
    db.commit()
    print(f"投递结果: accepted={accepted} duplicate={duplicated} rejected={rejected}")

    # 4) 离线对账（绝不触链）
    result = recon_svc.run_reconciliation(db)
    db.commit()
    print(f"\n对账 run_id : {result['run_id']}")
    print(f"快照文件    : {result['snapshot_path']}")
    print(f"快照哈希    : {result['snapshot_hash']}")
    print(f"前一快照哈希: {result['prev_snapshot_hash'] or '(创世)'}")
    print(f"findings    : {result['finding_count']}")
    for f in result["findings"]:
        print(f"  - [{f['severity']}] {f['code']}: {f['detail']}")
        print(f"      evidence: {f['evidence_path']}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
