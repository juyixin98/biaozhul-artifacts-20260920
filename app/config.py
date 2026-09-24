"""Runtime configuration.

Everything points at a *local* Anvil node and Anvil's well-known test
accounts by default. No mainnet keys or URLs are ever used.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Anvil's deterministic test accounts (anvil -a 10). The first account is the
# default deployer/sender; these keys are public knowledge and only valid on a
# local ephemeral chain.
ANVIL_TEST_PRIVATE_KEYS: tuple[str, ...] = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
)

ANVIL_DEFAULT_RPC = "http://127.0.0.1:8545"
ANVIL_DEFAULT_CHAIN_ID = 31337


@dataclass(frozen=True)
class Settings:
    rpc_url: str
    chain_id: int
    private_key: str
    contract_address: str | None

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            rpc_url=os.getenv("RPC_URL", ANVIL_DEFAULT_RPC),
            chain_id=int(os.getenv("CHAIN_ID", str(ANVIL_DEFAULT_CHAIN_ID))),
            private_key=os.getenv("SENDER_PRIVATE_KEY", ANVIL_TEST_PRIVATE_KEYS[0]),
            contract_address=(os.getenv("CONTRACT_ADDRESS") or None),
        )


def get_settings() -> Settings:
    return Settings.from_env()
