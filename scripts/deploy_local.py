#!/usr/bin/env python3
"""Deploy MockERC20 + ShareVault to a local Anvil chain and write
deployment.local.json (gitignored) for the backend to load.

Usage:
    python scripts/deploy_local.py                 # uses RPC_URL / PRIVATE_KEY env or Anvil defaults
"""
import json
import os
import sys
import time
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
ABI_DIR = ROOT / "backend" / "app" / "abi"
DEPLOYMENT_FILE = Path(os.environ.get("DEPLOYMENT_FILE", ROOT / "deployment.local.json"))

RPC_URL = os.environ.get("RPC_URL", "http://127.0.0.1:8545")
# Anvil deterministic account #0. LOCAL TEST KEY ONLY — never use elsewhere.
PRIVATE_KEY = os.environ.get(
    "PRIVATE_KEY",
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
)


def load_artifact(name: str) -> dict:
    return json.loads((ABI_DIR / f"{name}.json").read_text())


def deploy(w3: Web3, artifact: dict, *args) -> str:
    contract = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"])
    account = w3.eth.account.from_key(PRIVATE_KEY)
    tx = contract.constructor(*args).build_transaction(
        {"from": account.address, "nonce": w3.eth.get_transaction_count(account.address)}
    )
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status != 1:
        raise RuntimeError(f"deployment reverted: {tx_hash.hex()}")
    return receipt.contractAddress


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        sys.exit(f"cannot connect to {RPC_URL} — is anvil running?")
    account = w3.eth.account.from_key(PRIVATE_KEY)
    w3.eth.default_account = account.address
    print(f"connected: chain_id={w3.eth.chain_id} deployer={account.address}")

    token_addr = deploy(w3, load_artifact("MockERC20"))
    print(f"MockERC20:  {token_addr}")
    vault_addr = deploy(w3, load_artifact("ShareVault"), token_addr, "Vault Share", "vMOCK")
    print(f"ShareVault: {vault_addr}")

    deployment = {
        "rpc_url": RPC_URL,
        "chain_id": w3.eth.chain_id,
        "deployer": account.address,
        "asset": token_addr,
        "vault": vault_addr,
        "deployed_at": int(time.time()),
    }
    DEPLOYMENT_FILE.write_text(json.dumps(deployment, indent=2))
    print(f"wrote {DEPLOYMENT_FILE}")


if __name__ == "__main__":
    main()
