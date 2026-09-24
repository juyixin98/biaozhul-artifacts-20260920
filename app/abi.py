"""ABI loading and event decoding for the Vault contract."""
from __future__ import annotations

import json
import pathlib
from typing import Any

from web3 import Web3

# Events we index. Only these two ABI entries are required to decode logs;
# the full artifact ABI is loaded for transaction encoding.
VAULT_EVENTS_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "who", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
        ],
        "name": "Deposited",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "who", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
        ],
        "name": "Withdrawn",
        "type": "event",
    },
]

VAULT_FUNCTIONS_ABI = [
    {
        "inputs": [],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "stats",
        "outputs": [
            {"name": "deposited", "type": "uint256"},
            {"name": "withdrawn", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

VAULT_ABI = VAULT_EVENTS_ABI + VAULT_FUNCTIONS_ABI


def load_artifact(path: str | pathlib.Path | None = None) -> dict[str, Any]:
    """Load the forge-generated Vault.json artifact (bytecode + ABI)."""
    if path is None:
        path = pathlib.Path(__file__).resolve().parent.parent / "out" / "Vault.sol" / "Vault.json"
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def make_vault_contract(w3: Web3, address: str):
    """Return a web3 contract handle at *address* using the bundled ABI."""
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=VAULT_ABI)


def decode_log(w3: Web3, log: dict[str, Any]) -> tuple[str, str, int]:
    """Decode a Vault event log via ``process_log``.

    Returns ``(event_name, who_checksum_hex, amount_int)``.
    Raises KeyError/ValueError for logs from another contract or unknown topic.
    """
    selector = log["topics"][0]
    name_by_sig = {
        Web3.keccak(text="Deposited(address,uint256)").to_0x_hex(): "Deposited",
        Web3.keccak(text="Withdrawn(address,uint256)").to_0x_hex(): "Withdrawn",
    }
    name = name_by_sig[selector]
    # process_log works on an unconnected contract handle; address is only used
    # for the log's `address` field matching (Anvil logs already match Vault).
    contract = w3.eth.contract(address=Web3.to_checksum_address(log["address"]), abi=VAULT_ABI)
    parsed = getattr(contract.events, name)().process_log(log)
    who = Web3.to_checksum_address(parsed["args"]["who"])
    amount = int(parsed["args"]["amount"])
    return name, who, amount
