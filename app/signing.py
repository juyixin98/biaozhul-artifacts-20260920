"""EIP-712 订单签名，与合约 ORDER_TYPEHASH 严格一致。"""
from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data

DOMAIN_NAME = "BoundedSettlement"
DOMAIN_VERSION = "1"

ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "maker", "type": "address"},
        {"name": "sellToken", "type": "address"},
        {"name": "buyToken", "type": "address"},
        {"name": "sellAmount", "type": "uint256"},
        {"name": "buyAmount", "type": "uint256"},
        {"name": "feeCap", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "expiry", "type": "uint256"},
    ],
}

# 合约 ABI 中 tuple 字段顺序（必须与 Solidity struct 一致）
ORDER_KEYS = (
    "maker",
    "sellToken",
    "buyToken",
    "sellAmount",
    "buyAmount",
    "feeCap",
    "nonce",
    "expiry",
)


def order_to_tuple(order: dict) -> tuple:
    return tuple(order[k] for k in ORDER_KEYS)


def build_domain(chain_id: int, verifying_contract: str) -> dict:
    return {
        "name": DOMAIN_NAME,
        "version": DOMAIN_VERSION,
        "chainId": chain_id,
        "verifyingContract": verifying_contract,
    }


def sign_order(private_key: str, chain_id: int, verifying_contract: str, order: dict) -> str:
    """对订单做 EIP-712 签名，返回 0x 前缀的 65 字节签名。"""
    signable = encode_typed_data(
        domain_data=build_domain(chain_id, verifying_contract),
        message_types={"Order": ORDER_TYPES["Order"]},
        message_data={k: order[k] for k in ORDER_KEYS},
    )
    signed = Account.sign_message(signable, private_key)
    return "0x" + signed.signature.hex()
