"""EIP-712 signing helpers.

Two different hashes live in this project:

* **op id**  - what the contract stores operations under. Binds
  (target, value, dataHash, nonce, deadline). Stable across signer changes.
* **digest** - what signers actually sign. The same fields PLUS
  ``configVersion``, so a signature is only valid under the signer set that
  was active when it was produced.
"""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_typing import HexStr
from hexbytes import HexBytes

DOMAIN_NAME = "MultisigTimelock"
DOMAIN_VERSION = "1"

OP_TYPES = {
    "Op": [
        {"name": "target", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "dataHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "configVersion", "type": "uint256"},
    ]
}

SET_SIGNERS_TYPES = {
    "SetSigners": [
        {"name": "signers", "type": "address[]"},
        {"name": "threshold", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "configVersion", "type": "uint256"},
    ]
}


def domain(chain_id: int, verifying_contract: str) -> dict:
    return {
        "name": DOMAIN_NAME,
        "version": DOMAIN_VERSION,
        "chainId": chain_id,
        "verifyingContract": verifying_contract,
    }


def _sign(private_key: str, chain_id: int, contract_address: str,
          types: dict, message: dict) -> HexBytes:
    signed = Account.sign_message(
        encode_typed_data(
            domain_data=domain(chain_id, contract_address),
            message_types=types,
            message_data=message,
        ),
        private_key,
    )
    return HexBytes(signed.signature)  # r||s||v, 65 bytes


def _as_hex(sig: HexBytes) -> HexStr:
    # hexbytes >= 0.3 returns a 0x-prefixed string from .hex()
    return HexStr(sig.hex())


def sign_op(private_key: str, chain_id: int, contract_address: str,
            target: str, value: int, data: bytes | str, nonce: int,
            deadline: int, config_version: int) -> HexStr:
    """Sign an operation approval. Returns 0x-prefixed 65-byte signature."""
    from web3 import Web3

    if isinstance(data, str):
        data = HexBytes(data)
    return sign_op_digest(
        private_key, chain_id, contract_address, target, value,
        Web3.keccak(data), nonce, deadline, config_version,
    )


def sign_op_digest(private_key: str, chain_id: int, contract_address: str,
                   target: str, value: int, data_hash: bytes, nonce: int,
                   deadline: int, config_version: int) -> HexStr:
    """Sign an operation approval when only the calldata hash is known."""
    message = {
        "target": target,
        "value": value,
        "dataHash": data_hash,
        "nonce": nonce,
        "deadline": deadline,
        "configVersion": config_version,
    }
    return _as_hex(
        _sign(private_key, chain_id, contract_address, OP_TYPES, message)
    )


def sign_set_signers(private_key: str, chain_id: int, contract_address: str,
                     signers: list[str], threshold: int, nonce: int,
                     deadline: int, config_version: int) -> HexStr:
    """Sign a signer-set change approval."""
    message = {
        "signers": signers,
        "threshold": threshold,
        "nonce": nonce,
        "deadline": deadline,
        "configVersion": config_version,
    }
    return _as_hex(
        _sign(
            private_key, chain_id, contract_address, SET_SIGNERS_TYPES, message
        )
    )
