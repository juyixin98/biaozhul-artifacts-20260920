"""Static configuration: paths, well-known Anvil test keys, settings.

Only ever used against a local Anvil instance with publicly documented test
keys - never against a real network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENTS_FILE = ROOT / "deployments.json"

# Public, well-known Anvil test private keys (index 0..9). DO NOT fund these
# on any real network - everyone knows them.
ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1582e",
    "0x6370fd033278c143179d81c5526140625662b8daa446c22ee2d73face50415d2",
    "0xd497455f67e08f252523d235c46f549d2702736fb9675308595f7683880d9621",
    "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bcaf7245fb59d6f3c399e446f9",
]

ARTIFACTS = {
    "MultisigTimelock": "out/MultisigTimelock.sol/MultisigTimelock.json",
    "Counter": "out/Targets.sol/Counter.json",
    "FlakyTarget": "out/Targets.sol/FlakyTarget.json",
    "AlwaysFail": "out/Targets.sol/AlwaysFail.json",
    "ReentrantTarget": "out/Targets.sol/ReentrantTarget.json",
}


@dataclass
class Settings:
    """Runtime settings, overridable by environment variables."""

    rpc_url: str = field(
        default_factory=lambda: os.environ.get("RPC_URL", "http://127.0.0.1:8545")
    )
    chain_id: int = field(default_factory=lambda: int(os.environ.get("CHAIN_ID", "31337")))
    deployments_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("DEPLOYMENTS_PATH", str(DEPLOYMENTS_FILE))
        )
    )
    # Keys the service may sign operations with. Default: the first three
    # Anvil keys, matching the 2-of-3 constructor config used by deploy.
    signer_keys: list[str] = field(default_factory=list)
    # Key paying gas for all service transactions (need not be a signer).
    operator_key: str = field(default="")
    enable_dev_endpoints: bool = field(
        default_factory=lambda: os.environ.get("ENABLE_DEV_ENDPOINTS", "1") == "1"
    )
    host: str = field(default_factory=lambda: os.environ.get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "8000")))

    def __post_init__(self) -> None:
        env_keys = os.environ.get("SIGNER_KEYS")
        if env_keys:
            self.signer_keys = [k.strip() for k in env_keys.split(",") if k.strip()]
        else:
            self.signer_keys = ANVIL_KEYS[:3]
        if not self.operator_key:
            self.operator_key = os.environ.get("OPERATOR_KEY", ANVIL_KEYS[3])
