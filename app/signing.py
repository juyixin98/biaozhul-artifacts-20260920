"""EIP-712 signing for bounded orders.

Implemented with raw ``eth_abi`` encoding (no high-level typed-data helper) so
the digest is computed identically to the Solidity ``hashOrder``:

    digest = keccak256(0x1901 ‖ domainSeparator ‖ hashStruct(Order))

The Solidity struct and ``ORDER_FIELDS`` below MUST keep identical field names,
types, and order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from eth_abi import encode as abi_encode
from eth_keys import keys as eth_keys
from eth_utils import keccak

DOMAIN_NAME = "BoundedOrderSettlement"
DOMAIN_VERSION = "1"

# Mirror of src/BoundedOrderSettlement.sol ORDER_TYPEHASH (types, in order).
ORDER_FIELDS = [
    ("maker", "address"),
    ("taker", "address"),
    ("makerToken", "address"),
    ("takerToken", "address"),
    ("makerAmount", "uint256"),
    ("takerAmount", "uint256"),
    ("nonce", "uint256"),
    ("deadline", "uint256"),
    ("feeRecipient", "address"),
    ("maxFeeAmount", "uint256"),
]

ORDER_KEYS = [name for name, _ in ORDER_FIELDS]
ORDER_ABI_TYPES = [typ for _, typ in ORDER_FIELDS]

ORDER_TYPE_SIGNATURE = (
    "Order(" + ",".join(f"{typ} {name}" for name, typ in ORDER_FIELDS) + ")"
)
ORDER_TYPEHASH = keccak(text=ORDER_TYPE_SIGNATURE)

_DOMAIN_TYPE_SIGNATURE = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_DOMAIN_TYPEHASH = keccak(text=_DOMAIN_TYPE_SIGNATURE)


@dataclass
class Order:
    maker: str
    taker: str
    makerToken: str
    takerToken: str
    makerAmount: int
    takerAmount: int
    nonce: int
    deadline: int
    feeRecipient: str
    maxFeeAmount: int

    def to_tuple(self) -> tuple:
        """ABI tuple for web3.py contract calls (declaration order)."""
        d = asdict(self)
        return tuple(d[k] for k in ORDER_KEYS)

    def to_message(self) -> dict[str, Any]:
        return asdict(self)


def _struct_hash(order: Order) -> bytes:
    values = (ORDER_TYPEHASH,) + tuple(
        getattr(order, name) for name in ORDER_KEYS
    )
    return keccak(abi_encode(["bytes32"] + ORDER_ABI_TYPES, values))


def _domain_separator(chain_id: int, verifying_contract: str) -> bytes:
    return keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            (
                _DOMAIN_TYPEHASH,
                keccak(text=DOMAIN_NAME),
                keccak(text=DOMAIN_VERSION),
                chain_id,
                verifying_contract,
            ),
        )
    )


def order_digest(chain_id: int, verifying_contract: str, order: Order) -> bytes:
    """Raw 32-byte EIP-712 digest the contract recovers the signer from."""
    return keccak(
        b"\x19\x01"
        + _domain_separator(chain_id, verifying_contract)
        + _struct_hash(order)
    )


def order_hash(chain_id: int, verifying_contract: str, order: Order) -> str:
    """0x-prefixed hex digest, identical to the contract's hashOrder()."""
    return "0x" + order_digest(chain_id, verifying_contract, order).hex()


def sign_order(private_key: str, chain_id: int, verifying_contract: str, order: Order) -> str:
    """Sign the EIP-712 digest; return a 65-byte hex signature r||s||v."""
    digest = order_digest(chain_id, verifying_contract, order)
    # Raw ECDSA over the 32-byte EIP-712 digest (already 0x1901-prefixed).
    # eth_keys yields a low-s signature; v is 27/28 for the contract's
    # ecrecover.
    pk = eth_keys.PrivateKey(bytes.fromhex(private_key.removeprefix("0x")))
    sig = pk.sign_msg_hash(digest)
    raw = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v + 27])
    return raw.hex()


def domain(chain_id: int, verifying_contract: str) -> dict[str, Any]:
    """Kept for API/diagnostics convenience."""
    return {
        "name": DOMAIN_NAME,
        "version": DOMAIN_VERSION,
        "chainId": chain_id,
        "verifyingContract": verifying_contract,
    }


def full_message(chain_id: int, verifying_contract: str, order: Order) -> dict[str, Any]:
    """EIP-712 typed-data document (useful for external tooling / docs)."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [{"name": n, "type": t} for n, t in ORDER_FIELDS],
        },
        "primaryType": "Order",
        "domain": domain(chain_id, verifying_contract),
        "message": order.to_message(),
    }
