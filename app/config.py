"""配置：全部通过环境变量覆盖，默认值指向本机 Anvil。"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Anvil 启动时打印的第一个测试私钥（公开测试密钥，仅限本地开发）。
DEFAULT_ANVIL_PRIVATE_KEY = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)


@dataclass(frozen=True)
class Settings:
    rpc_url: str
    private_key: str
    contract_address: str | None
    abi_path: str
    # 等待交易回执的秒数
    receipt_timeout: float

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            rpc_url=os.getenv("RPC_URL", "http://127.0.0.1:8545"),
            private_key=os.getenv("PRIVATE_KEY", DEFAULT_ANVIL_PRIVATE_KEY),
            contract_address=os.getenv("CONTRACT_ADDRESS") or None,
            abi_path=os.getenv("ABI_PATH", "out/Checkpoints.sol/Checkpoints.json"),
            receipt_timeout=float(os.getenv("RECEIPT_TIMEOUT", "30")),
        )
