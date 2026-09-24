"""web3.py client: chain connection, contract handles, transaction helpers."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractCustomError, ContractLogicError

from .config import get_settings


class ChainError(Exception):
    """Raised when an on-chain call/transaction reverts."""


@lru_cache
def get_web3() -> Web3:
    settings = get_settings()
    w3 = Web3(Web3.HTTPProvider(settings.rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ChainError(f"cannot connect to RPC {settings.rpc_url}")
    account = w3.eth.account.from_key(settings.private_key)
    w3.eth.default_account = account.address
    return w3


def _load_abi(name: str) -> list[dict[str, Any]]:
    settings = get_settings()
    return json.loads((settings.abi_dir / f"{name}.json").read_text())["abi"]


@lru_cache
def get_contracts() -> tuple[Contract, Contract]:
    """Returns (vault, asset) bound to the deployed addresses."""
    settings = get_settings()
    w3 = get_web3()
    deployment = settings.load_deployment()
    vault = w3.eth.contract(
        address=Web3.to_checksum_address(deployment["vault"]), abi=_load_abi("ShareVault")
    )
    asset = w3.eth.contract(
        address=Web3.to_checksum_address(deployment["asset"]), abi=_load_abi("MockERC20")
    )
    return vault, asset


def server_address() -> str:
    return get_web3().eth.default_account


def call(fn, *args) -> Any:
    """eth_call with revert decoding."""
    try:
        return fn(*args).call()
    except (ContractLogicError, ContractCustomError, ValueError) as exc:
        raise ChainError(f"call reverted: {exc}") from exc


def transact(fn, *args) -> dict:
    """Build, locally sign, send, and wait for a transaction. Reverts surface
    as ChainError with the decoded reason."""
    w3 = get_web3()
    settings = get_settings()
    account = w3.eth.account.from_key(settings.private_key)
    try:
        tx = fn(*args).build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
            }
        )
    except (ContractLogicError, ContractCustomError, ValueError) as exc:
        raise ChainError(f"transaction reverted: {exc}") from exc
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status != 1:
        raise ChainError(f"transaction failed on-chain: {tx_hash.hex()}")
    return {
        "tx_hash": tx_hash.hex(),
        "block_number": receipt.blockNumber,
        "gas_used": receipt.gasUsed,
        "receipt": receipt,
    }
