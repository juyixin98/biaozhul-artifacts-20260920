"""应用配置：全部来自环境变量（12-factor），不使用 .env 文件，便于容器化。"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Anvil 默认第一个测试私钥（公开的确定性测试密钥，仅用于本地链）
DEFAULT_PRIVATE_KEY = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)
DEFAULT_RPC_URL = "http://127.0.0.1:8545"
DEFAULT_ALLOCATIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "allocations.json",
)
DEFAULT_ARTIFACT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "out",
    "MerkleClaim.sol",
    "MerkleClaim.json",
)


@dataclass(frozen=True)
class Settings:
    rpc_url: str
    contract_address: str | None
    deployer_private_key: str
    allocations_path: str
    artifact_path: str
    # 谁来发领取交易：默认用后端 deployer 密钥（任何账户都可代领）
    claimer_private_key: str

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            rpc_url=os.getenv("RPC_URL", DEFAULT_RPC_URL),
            contract_address=os.getenv("CONTRACT_ADDRESS"),
            deployer_private_key=os.getenv("DEPLOYER_PRIVATE_KEY", DEFAULT_PRIVATE_KEY),
            claimer_private_key=os.getenv("CLAIMER_PRIVATE_KEY", os.getenv("DEPLOYER_PRIVATE_KEY", DEFAULT_PRIVATE_KEY)),
            allocations_path=os.getenv("ALLOCATIONS_PATH", DEFAULT_ALLOCATIONS),
            artifact_path=os.getenv("ARTIFACT_PATH", DEFAULT_ARTIFACT),
        )
