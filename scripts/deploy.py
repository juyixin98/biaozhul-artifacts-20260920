"""Deploy the Ledger contract to a local Anvil node.

Usage:
    python -m scripts.deploy
Env:
    INDEXER_RPC_URL   default http://127.0.0.1:8545
    DEPLOYER_KEY      default Anvil test key #0
Prints the deployed address and a ready-to-export LEDGER_ADDRESS line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from eth_utils import to_checksum_address
from web3 import Web3

from app.config import DEFAULT_ANVIL_RPC, DEFAULT_TEST_KEY


def creation_bytecode() -> bytes:
    artifact = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts/out/Ledger.sol/Ledger.json").read_text()
    )
    return bytes.fromhex(artifact["bytecode"]["object"][2:])


def deploy(rpc_url: str, key: str) -> str:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise SystemExit(f"cannot reach Anvil at {rpc_url}")
    acct = w3.eth.account.from_key(key)
    tx = {
        "from": acct.address,
        "data": creation_bytecode(),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 2_000_000,
        "maxFeePerGas": w3.to_wei(20, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
        "chainId": w3.eth.chain_id,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise SystemExit("deployment transaction reverted")
    return to_checksum_address(receipt["contractAddress"])


def main() -> None:
    rpc = os.getenv("INDEXER_RPC_URL", DEFAULT_ANVIL_RPC)
    key = os.getenv("DEPLOYER_KEY", DEFAULT_TEST_KEY)
    address = deploy(rpc, key)
    print(f"Deployed Ledger at {address}")
    print(f"export LEDGER_ADDRESS={address}")


if __name__ == "__main__":
    main()
