"""Runtime configuration for the vault backend.

Everything is environment-driven with Anvil-local defaults. The default key is
Anvil's well-known deterministic account #0 — a LOCAL TEST KEY ONLY.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Anvil deterministic account #0. Never fund this key on a real network.
DEFAULT_ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class Settings:
    def __init__(self) -> None:
        self.rpc_url: str = os.environ.get("RPC_URL", "http://127.0.0.1:8545")
        self.private_key: str = os.environ.get("PRIVATE_KEY", DEFAULT_ANVIL_KEY)
        self.deployment_file: Path = Path(
            os.environ.get("DEPLOYMENT_FILE", str(ROOT / "deployment.local.json"))
        )
        self.abi_dir: Path = Path(os.environ.get("ABI_DIR", str(Path(__file__).parent / "abi")))

    def load_deployment(self) -> dict:
        return json.loads(self.deployment_file.read_text())


@lru_cache
def get_settings() -> Settings:
    return Settings()
