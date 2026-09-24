"""web3.py helpers for talking to a local Anvil chain over HTTP.

Only local test keys are used — never point this at a real network.
"""

from __future__ import annotations

from web3 import Web3
from web3.contract import Contract

DEFAULT_RPC_URL = "http://127.0.0.1:8545"

# Anvil/Hardhat well-known test account #0 (public, do not reuse elsewhere).
TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def connect(rpc_url: str = DEFAULT_RPC_URL) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError(f"cannot connect to {rpc_url}; is anvil running?")
    return w3


def _sign_and_send(w3: Web3, tx: dict, private_key: str = TEST_PRIVATE_KEY):
    account = w3.eth.account.from_key(private_key)
    tx.setdefault("from", account.address)
    tx.setdefault("nonce", w3.eth.get_transaction_count(account.address))
    tx.setdefault("chainId", w3.eth.chain_id)
    tx.setdefault("gas", 3_000_000)
    tx.setdefault("maxFeePerGas", w3.to_wei(2, "gwei"))
    tx.setdefault("maxPriorityFeePerGas", w3.to_wei(1, "gwei"))
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status != 1:
        raise RuntimeError(f"transaction {tx_hash.hex()} reverted")
    return receipt


def deploy(
    w3: Web3, artifact: dict, *constructor_args, private_key: str = TEST_PRIVATE_KEY
) -> Contract:
    """Deploy a compiled artifact and return a bound contract instance."""
    factory = w3.eth.contract(
        abi=artifact["abi"], bytecode=artifact["bytecode"]["object"]
    )
    tx = factory.constructor(*constructor_args).build_transaction(
        {"from": w3.eth.account.from_key(private_key).address}
    )
    receipt = _sign_and_send(w3, tx, private_key)
    return w3.eth.contract(address=receipt.contractAddress, abi=artifact["abi"])


def transact(w3: Web3, fn, *args, private_key: str = TEST_PRIVATE_KEY):
    """Build, sign, send and confirm a state-changing contract call."""
    sender = w3.eth.account.from_key(private_key).address
    tx = fn(*args).build_transaction({"from": sender})
    return _sign_and_send(w3, tx, private_key)
