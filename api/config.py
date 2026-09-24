"""Local-chain configuration for the vesting API and integration tests.

Everything defaults to a local Anvil node and Anvil's well-known
deterministic test keys. Do NOT use these keys anywhere outside local
development.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out"

RPC_URL = os.environ.get("RPC_URL", "http://127.0.0.1:8545")
CHAIN_ID = int(os.environ.get("CHAIN_ID", "31337"))

# Anvil's first two pre-funded test accounts (owner / beneficiary).
ANVIL_KEY_0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_KEY_1 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

OWNER_PRIVATE_KEY = os.environ.get("OWNER_PRIVATE_KEY", ANVIL_KEY_0)
BENEFICIARY_PRIVATE_KEY = os.environ.get("BENEFICIARY_PRIVATE_KEY", ANVIL_KEY_1)

TOKEN_ADDRESS = os.environ.get("TOKEN_ADDRESS", "")
VESTING_ADDRESS = os.environ.get("VESTING_ADDRESS", "")

# Contract addresses written by scripts/deploy.sh (key -> address).
DEPLOYMENT_FILE = ROOT / "deploy" / "addresses.json"


def load_abi(contract_name: str) -> list:
    path = OUT_DIR / f"{contract_name}.sol" / f"{contract_name}.json"
    import json

    with path.open() as fh:
        artifact = json.load(fh)
    return artifact["abi"]


def load_deployed_addresses() -> dict[str, str]:
    """Read addresses produced by scripts/deploy.sh (env vars take priority)."""
    addresses: dict[str, str] = {}
    if DEPLOYMENT_FILE.exists():
        import json

        addresses = json.loads(DEPLOYMENT_FILE.read_text())
    if TOKEN_ADDRESS:
        addresses["token"] = TOKEN_ADDRESS
    if VESTING_ADDRESS:
        addresses["vesting"] = VESTING_ADDRESS
    return addresses
