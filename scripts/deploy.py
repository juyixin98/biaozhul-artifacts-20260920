#!/usr/bin/env python3
"""把 HTLC 合约部署到两条本地 anvil 链，并写 deployment.json。

前置：
    bash scripts/anvil-start.sh
    forge build

用法：
    .venv/bin/python scripts/deploy.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chain import ARTIFACT_PATH, LegClient  # noqa: E402
from app.config import (  # noqa: E402
    ALPHA_CHAIN_ID,
    ALPHA_RPC,
    ANVIL_TEST_KEYS,
    BETA_CHAIN_ID,
    BETA_RPC,
)

TARGETS = [
    ("alpha", ALPHA_RPC, ALPHA_CHAIN_ID),
    ("beta", BETA_RPC, BETA_CHAIN_ID),
]


def main() -> int:
    if not ARTIFACT_PATH.exists():
        print("缺少 out/ 构建产物，请先运行: forge build", file=sys.stderr)
        return 1

    manifest: dict[str, dict] = {}
    for name, rpc, chain_id in TARGETS:
        addr, block = LegClient.deploy(rpc, chain_id, ANVIL_TEST_KEYS[0])
        print(f"[{name}] rpc={rpc} chain_id={chain_id} HTLC={addr} (block {block})")
        manifest[name] = {
            "rpc_url": rpc,
            "chain_id": chain_id,
            "address": addr,
            "deploy_block": block,
        }

    Path("deployment.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("已写入 deployment.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
