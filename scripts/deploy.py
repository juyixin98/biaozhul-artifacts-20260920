"""Deploy Checkpoints to a local Anvil node and print the address.

Usage:
    python scripts/deploy.py
    CONTRACT_ADDRESS=0x... python scripts/deploy.py   # not needed; informational
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.contract import deploy  # noqa: E402


def main() -> None:
    settings = get_settings()
    w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
    if not w3.is_connected():
        raise SystemExit(
            f"Cannot reach Anvil at {settings.rpc_url}. "
            "Start it first: anvil --block-time 1"
        )

    artifact = json.loads((ROOT / "out" / "Checkpoints.sol" / "Checkpoints.json").read_text())
    bytecode = artifact["bytecode"]["object"]

    print(f"RPC        : {settings.rpc_url}")
    print(f"chain id   : {int(w3.eth.chain_id)}")
    print(f"deployer   : {w3.eth.account.from_key(settings.private_key).address}")
    cp = deploy(w3, settings.private_key, bytecode)
    print(f"deployed at: {cp.address}")
    print()
    print("Run the API against this contract with:")
    print(f"  CONTRACT_ADDRESS={cp.address} uvicorn app.main:app --port 8000")


if __name__ == "__main__":
    main()
