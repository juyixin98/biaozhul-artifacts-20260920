#!/usr/bin/env python3
"""把 MockERC20 + ShareVault 部署到本机 Anvil，并写入 deployments/local.json。

前置条件：
  1. 已执行 `forge build`（产生 out/ 产物）；
  2. 本地已启动 anvil（默认 http://127.0.0.1:8545）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接 `python scripts/deploy.py` 运行：把项目根加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import chain as chainlib  # noqa: E402
from backend.app.config import DEFAULT_PRIVATE_KEY, RPC_URL  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="部署合约到本机 Anvil")
    parser.add_argument("--rpc-url", default=RPC_URL)
    parser.add_argument("--private-key", default=DEFAULT_PRIVATE_KEY)
    args = parser.parse_args()

    result = chainlib.deploy_contracts(args.rpc_url, args.private_key)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
