"""链与测试账户配置。

警告：这里的私钥全部来自 anvil / Hardhat 公开确定性测试助记词，
**只能**用于本机测试链，绝不可用于任何真实网络。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# anvil 默认派生的前几个测试私钥（公开信息，仅本机测试用）
ANVIL_TEST_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # 0xf39F...2266
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",  # 0x7099...79C8
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",  # 0x3C44...1992
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",  # 0x90F7...e1D6
]

ALPHA_RPC = os.environ.get("HTLC_ALPHA_RPC", "http://127.0.0.1:8645")
BETA_RPC = os.environ.get("HTLC_BETA_RPC", "http://127.0.0.1:8646")
ALPHA_CHAIN_ID = int(os.environ.get("HTLC_ALPHA_CHAIN_ID", "31337"))
BETA_CHAIN_ID = int(os.environ.get("HTLC_BETA_CHAIN_ID", "31338"))
RPC_TIMEOUT = float(os.environ.get("HTLC_RPC_TIMEOUT", "2.0"))
DEFAULT_MANIFEST = os.environ.get("HTLC_DEPLOYMENT", "deployment.json")


@dataclass(frozen=True)
class ChainConfig:
    name: str
    rpc_url: str
    chain_id: int
    address: str | None = None
    deploy_block: int = 0


def load_manifest(path: str | os.PathLike[str] = DEFAULT_MANIFEST) -> dict[str, ChainConfig]:
    """读取 deploy.py 写出的部署清单。"""
    raw = json.loads(Path(path).read_text())
    out: dict[str, ChainConfig] = {}
    for leg in ("alpha", "beta"):
        item = raw[leg]
        out[leg] = ChainConfig(
            name=leg,
            rpc_url=item["rpc_url"],
            chain_id=int(item["chain_id"]),
            address=item["address"],
            deploy_block=int(item.get("deploy_block", 0)),
        )
    return out


def default_configs() -> dict[str, ChainConfig]:
    """尚未部署时使用的占位配置（协调器会报告合约未配置）。"""
    return {
        "alpha": ChainConfig("alpha", ALPHA_RPC, ALPHA_CHAIN_ID),
        "beta": ChainConfig("beta", BETA_RPC, BETA_CHAIN_ID),
    }


def role_keys() -> dict[str, dict[str, str]]:
    """交换角色与测试密钥的对应关系。

    典型跨链交换：Alice 在 Alpha 链上锁给 Bob，Bob 在 Beta 链上锁给 Alice，
    两侧 hashLock / swapId 相同。
    """
    return {
        "alpha": {"sender": ANVIL_TEST_KEYS[0], "receiver": ANVIL_TEST_KEYS[1]},
        "beta": {"sender": ANVIL_TEST_KEYS[1], "receiver": ANVIL_TEST_KEYS[0]},
    }
