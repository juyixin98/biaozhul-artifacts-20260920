"""后端配置：全部指向本机 Anvil，仅使用其自带测试密钥。"""
from __future__ import annotations

import os
from pathlib import Path

# 本机 Anvil JSON-RPC（HTTP）。
RPC_URL = os.environ.get("RPC_URL", "http://127.0.0.1:8545")

# 链 ID：Anvil 默认 31337。
CHAIN_ID = int(os.environ.get("CHAIN_ID", "31337"))

# Anvil 启动时打印的 10 个测试私钥（公开、无价值，仅限本地）。
ANVIL_TEST_PRIVATE_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",
    "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97",
    "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6",
]

# 默认操作者/部署者（Anvil 账户 #0）。
DEFAULT_PRIVATE_KEY = os.environ.get(
    "OPERATOR_PK", ANVIL_TEST_PRIVATE_KEYS[0]
)

# 每笔交易给用户计算滑点下界时的默认容忍度（基点，1bp = 0.01%）。
# 注意：链上滑点由 minShares/minAssetsOut 参数强制；这里只是后端默认值。
DEFAULT_SLIPPAGE_BPS = int(os.environ.get("DEFAULT_SLIPPAGE_BPS", "50"))  # 0.5%

# 项目路径与 forge 产物。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FORGE_OUT = PROJECT_ROOT / "out"
DEPLOYMENT_FILE = PROJECT_ROOT / "deployments" / "local.json"
