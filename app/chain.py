"""On-chain connection helpers for the vesting backend.

Talks to a local Anvil node over HTTP using web3.py. Nothing here assumes any
public network; the signing key and RPC URL come from environment variables and
default to Anvil's well-known test accounts.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# Project layout -------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out"
DEPLOYMENT_DIR = ROOT / "deployment"
ADDRESSES_FILE = DEPLOYMENT_DIR / "addresses.json"

# Defaults point at a local Anvil chain and its first prefunded test key.
DEFAULT_RPC = "http://127.0.0.1:8545"
# Anvil account #0 private key (publicly known, local development ONLY).
DEFAULT_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# Anvil's first 10 prefunded accounts (deterministic, never use real funds).
ANVIL_TEST_ACCOUNTS = [
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
    "0x90F79bf6EB2c4f870365E785982E1f101E93b90",
    "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65",
    "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
    "0x976EA74026E726554dB657fA54763abd0C3a0aa9",
    "0x14dC79964da2C08b23698B3D3cc7Ca32193d9955",
    "0x23618e81E3f5cdF7f54C3d65f7FBc0aBf5B21E8f",
    "0xa0Ee7A142d267C1f36714E4a8F75612F20a79720",
]


def rpc_url() -> str:
    return os.environ.get("RPC_URL", DEFAULT_RPC)


def get_web3() -> Web3:
    """Build a connected Web3 HTTP provider pointed at the local chain."""
    w3 = Web3(Web3.HTTPProvider(rpc_url(), request_kwargs={"timeout": 30}))
    # Anvil blocks carry a PoA-style extraData field; web3 7 needs this shim.
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise ConnectionError(f"cannot connect to Ethereum node at {rpc_url()}")
    return w3


def load_artifact(contract_name: str) -> dict[str, Any]:
    """Load a forge JSON artifact (contains `abi` and `bytecode`)."""
    path = OUT_DIR / f"{contract_name}.sol" / f"{contract_name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"artifact {path} not found; run `forge build` first"
        )
    with path.open() as fh:
        return json.load(fh)


def get_contract(w3: Web3, name: str, address: str | None = None):
    """Return a web3 contract handle. Deploys when no address is supplied."""
    artifact = load_artifact(name)
    return w3.eth.contract(
        address=Web3.to_checksum_address(address) if address else None,
        abi=artifact["abi"],
        bytecode=artifact["bytecode"]["object"]
        if isinstance(artifact["bytecode"], dict)
        else artifact["bytecode"],
    )


def load_addresses() -> dict[str, str]:
    if not ADDRESSES_FILE.exists():
        raise FileNotFoundError(
            f"{ADDRESSES_FILE} missing; run `python scripts/deploy.py` first"
        )
    with ADDRESSES_FILE.open() as fh:
        return json.load(fh)


def send_contract_tx(w3: Web3, account, func, *, gas_padding: float = 1.25) -> str:
    """Build, sign and send a state-changing contract function call.

    Uses legacy gas pricing (works on Anvil without an EIP-1559 fee oracle).
    Returns the transaction hash as a 0x-hex string.
    """
    tx = func.build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": int(func.estimate_gas({"from": account.address}) * gas_padding),
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    tx_hash = w3.eth.send_raw_transaction(raw)
    return tx_hash.hex()


def wait_receipt(w3: Web3, tx_hash: str, timeout: int = 60):
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)


def account_from_key(w3: Web3, private_key: str | None = None):
    pk = private_key or os.environ.get("PRIVATE_KEY", DEFAULT_PRIVATE_KEY)
    if not pk.startswith("0x"):
        pk = "0x" + pk
    return w3.eth.account.from_key(pk)


@lru_cache(maxsize=1)
def deployed_handles():
    """Cached (w3, token, vesting, deployer) tuple once addresses exist."""
    w3 = get_web3()
    addresses = load_addresses()
    token = get_contract(w3, "SyntheticToken", addresses["token"])
    vesting = get_contract(w3, "TokenVesting", addresses["vesting"])
    return w3, token, vesting
