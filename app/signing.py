"""Offline EVM transaction signing built on eth-account (eth-keys/coincurve).

Signatures are deterministic (RFC 6979), so re-signing the same transaction
with the same key always yields the identical signed payload — this is what
makes crash-recovery retries safe. No custom cryptography lives here.
"""

from eth_account import Account


def address_from_private_key(private_key_hex: str) -> str:
    return Account.from_key(private_key_hex).address


def generate_private_key() -> str:
    return Account.create().key.hex()


def sign_evm_transaction(
    private_key_hex: str,
    *,
    chain_id: int,
    to: str,
    value_wei: int,
    gas_limit: int,
    gas_price_wei: int,
    nonce: int,
) -> tuple[str, str]:
    """Sign a legacy EVM transaction offline. Returns (raw_tx_hex, tx_hash_hex)."""
    tx = {
        "chainId": chain_id,
        "to": to,
        "value": value_wei,
        "gas": gas_limit,
        "gasPrice": gas_price_wei,
        "nonce": nonce,
        "data": b"",
    }
    signed = Account.sign_transaction(tx, private_key_hex)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    tx_hash = getattr(signed, "hash", None) or getattr(signed, "transactionHash")
    raw_hex = raw.hex()
    hash_hex = tx_hash.hex()
    return (
        raw_hex if raw_hex.startswith("0x") else "0x" + raw_hex,
        hash_hex if hash_hex.startswith("0x") else "0x" + hash_hex,
    )


def recover_sender(raw_tx_hex: str) -> str:
    """Offline verification: recover the signer address from a signed raw tx."""
    return Account.recover_transaction(raw_tx_hex)
