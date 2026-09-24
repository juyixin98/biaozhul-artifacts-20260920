"""后端配置（全部可通过环境变量覆盖）。"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 本地 Anvil 默认地址；题目要求 HTTP 接口只连本机链
RPC_URL = os.environ.get("RPC_URL", "http://127.0.0.1:8545")

# Anvil 自带测试账户 #0 的私钥（仅用于本地测试，切勿用于主网）
DEFAULT_ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PRIVATE_KEY = os.environ.get("PRIVATE_KEY", DEFAULT_ANVIL_KEY)

# 已部署合约地址；也可由部署信息文件自动读取
CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "").strip()
DEPLOY_INFO = Path(os.environ.get("DEPLOY_INFO", str(REPO_ROOT / "deployments" / "anvil.json")))

# 编译产物（forge build 生成）
ARTIFACT_PATH = Path(
    os.environ.get("ARTIFACT_PATH", str(REPO_ROOT / "out" / "MerkleClaim.sol" / "MerkleClaim.json"))
)

# 启动时自动加载的分配表（可选）
ALLOCATIONS_FILE = os.environ.get("ALLOCATIONS_FILE", "").strip()

# 发送交易参数
GAS_LIMIT = int(os.environ.get("GAS_LIMIT", "3_000_000"))
TX_TIMEOUT = int(os.environ.get("TX_TIMEOUT", "60"))
