"""Offline EVM transaction signing and offline verification.

Uses eth-account (part of the Ethereum Foundation web3.py stack) with the
system secp256k1 / keccak primitives it vendors. No network access happens
anywhere in this module: nothing is broadcast and no RPC is contacted.

Only EIP-155 legacy transactions are produced, because their (v,r,s)+RLP form
can be verified by anyone with no chain access:
    recovered = Account.recover_transaction(raw_hex)
"""
from __future__ import annotations

import dataclasses

from eth_account import Account
from eth_account.datastructures import SignedTransaction
from eth_utils import keccak


@dataclasses.dataclass(frozen=True)
class TxContent:
    chain_id: int
    to_address: str
    value_wei: int
    gas: int
    gas_price_wei: int
    nonce: int
    data: bytes


@dataclasses.dataclass(frozen=True)
class SignedResult:
    raw_transaction_hex: str
    tx_hash: str
    v: int
    r: int
    s: int


def _decode_data(data_hex: str | None) -> bytes:
    if data_hex is None or data_hex in ("", "0x", "0X"):
        return b""
    h = data_hex[2:] if data_hex.startswith(("0x", "0X")) else data_hex
    try:
        return bytes.fromhex(h)
    except ValueError:
        raise ValueError("data must be hexadecimal bytes") from None


def sign_legacy_tx(private_key: bytes, tx: TxContent) -> SignedResult:
    """Produce a deterministic (key,tx)->signature EIP-155 legacy signature."""
    payload = {
        "chainId": tx.chain_id,
        "to": tx.to_address,
        "value": tx.value_wei,
        "gas": tx.gas,
        "gasPrice": tx.gas_price_wei,
        "nonce": tx.nonce,
        "data": tx.data,
    }
    signed: SignedTransaction = Account.sign_transaction(payload, private_key)
    raw = bytes(signed.rawTransaction)
    tx_hash = keccak(raw)
    return SignedResult(
        raw_transaction_hex="0x" + raw.hex(),
        tx_hash="0x" + tx_hash.hex(),
        v=signed.v,
        r=signed.r,
        s=signed.s,
    )


def recover_signer(raw_transaction_hex: str) -> str:
    """Offline verification: recover the checksum address that signed `raw`."""
    return Account.recover_transaction(raw_transaction_hex)


def decode_unsigned_payload(raw_transaction_hex: str) -> dict:
    """Decode a legacy signed RLP transaction into its fields, for offline
    verification against the requested content. Works without any chain."""
    import rlp
    from eth_account._utils.legacy_transactions import Transaction

    hx = raw_transaction_hex[2:] if raw_transaction_hex.startswith(("0x", "0X")) else raw_transaction_hex
    decoded = rlp.decode(bytes.fromhex(hx), Transaction)
    to_bytes = bytes(decoded.to) if decoded.to is not None else b""
    data = bytes(decoded.data) if decoded.data is not None else b""
    return {
        "nonce": int(decoded.nonce),
        "gas_price_wei": int(decoded.gasPrice),
        "gas": int(decoded.gas),
        "to_address": ("0x" + to_bytes.hex()) if to_bytes else "",
        "value_wei": int(decoded.value),
        "data_hex": ("0x" + data.hex()) if data else "0x",
        "v": int(decoded.v),
        "r": int(decoded.r),
        "s": int(decoded.s),
    }


def derive_address(private_key: bytes) -> str:
    return Account.from_key(private_key).address


def address_from_hex(private_key_hex: str) -> str:
    hx = private_key_hex[2:] if private_key_hex.startswith(("0x", "0X")) else private_key_hex
    return derive_address(bytes.fromhex(hx))
