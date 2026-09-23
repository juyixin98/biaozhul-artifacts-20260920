"""资产/消息标识与金额：防跨链碰撞的全局身份。"""
from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from . import identity
from .crypto import hash_canonical

VALID_CHAIN = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
VALID_EVENT = {"LOCK", "MINT", "BURN", "RELEASE"}


def parse_amount(value: Any) -> int:
    """金额必须是非负整数（字符串或整数传输，避免浮点）。"""
    if isinstance(value, bool):
        raise ValueError("amount must be a non-negative integer, not bool")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str):
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError("amount string must be a non-negative base-10 integer")
        amount = int(value)
    else:
        raise ValueError("amount must be a non-negative integer or decimal string")
    if amount < 0:
        raise ValueError("amount must be non-negative")
    return amount


def asset_uid(source_chain: str, original_contract: str, token_id: str) -> str:
    """全局资产标识 = sha256(域 | 源链 | 原合约 | tokenID)。

    两条链上相同合约地址，因 source_chain 不同而得到不同 UID，避免跨链碰撞。
    """
    return hash_canonical(
        ["asset-uid/v1", source_chain, original_contract, token_id]
    )


def message_id(source_chain: str, source_nonce: str) -> str:
    """跨链关联消息 ID：由源链与源链序号决定。"""
    return hash_canonical(["message-id/v1", source_chain, source_nonce])


def event_key(chain_id: str, tx_hash: str, log_index: int) -> str:
    """链上事件唯一键（幂等去重）。"""
    return hash_canonical(["event-key/v1", chain_id, tx_hash, log_index])


def validate_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验事件载荷并返回规范化字段（金额转 int，UID/消息ID 派生）。

    返回 dict 而不是 pydantic 模型，便于直接持久化。
    """
    required = {
        "chain_id", "event_type", "tx_hash", "log_index", "block_height",
        "block_hash", "source_chain", "original_contract", "token_id", "source_nonce",
        "account", "amount",
    }
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"payload missing fields: {sorted(missing)}")

    chain_id = str(payload["chain_id"])
    event_type = str(payload["event_type"]).upper()
    if not VALID_CHAIN.match(chain_id):
        raise ValueError("invalid chain_id")
    if event_type not in VALID_EVENT:
        raise ValueError(f"event_type must be one of {sorted(VALID_EVENT)}")

    try:
        log_index = int(payload["log_index"])
        block_height = int(payload["block_height"])
    except (TypeError, ValueError) as exc:
        raise ValueError("log_index/block_height must be integers") from exc
    if log_index < 0 or block_height < 0:
        raise ValueError("log_index/block_height must be non-negative")

    source_chain = str(payload["source_chain"])
    original_contract = str(payload["original_contract"])
    token_id = str(payload.get("token_id") or "")
    source_nonce = str(payload["source_nonce"])
    account = str(payload["account"])
    block_hash = str(payload["block_hash"])
    if not block_hash:
        raise ValueError("block_hash is required")
    if not original_contract:
        raise ValueError("original_contract is required")
    if not source_nonce:
        raise ValueError("source_nonce is required")
    if not account:
        raise ValueError("account is required")

    amount = parse_amount(payload["amount"])

    uid = identity.asset_uid(source_chain, original_contract, token_id)
    mid = identity.message_id(source_chain, source_nonce)
    ekey = identity.event_key(chain_id, str(payload["tx_hash"]), log_index)

    return {
        "chain_id": chain_id,
        "event_type": event_type,
        "tx_hash": str(payload["tx_hash"]),
        "log_index": log_index,
        "block_height": block_height,
        "block_hash": block_hash,
        "source_chain": source_chain,
        "original_contract": original_contract,
        "token_id": token_id,
        "source_nonce": source_nonce,
        "account": account,
        "amount": amount,
        "asset_uid": uid,
        "message_id": mid,
        "event_key": ekey,
    }
