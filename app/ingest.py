"""事件投递：真实验签 + 幂等入库（重复投递不重复计数）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import chains as chains_svc
from .crypto import canonical_json, sha256_hex, verify_signature
from .identity import validate_event_payload
from .models import Chain, ChainEvent, RawEvent


class IngestError(ValueError):
    """可对外展示的投递错误（HTTP 400）。"""


def ingest_event(db: Session, payload: dict, signature: str, feeder_public_key: str) -> dict:
    """校验、验签并幂等写入一条事件。

    返回 {"status": "accepted"|"duplicate", "event_key": ...}。
    payload 与 signature 由调用方从信封解出；feeder_public_key 必须是该链已登记公钥。
    """
    now = datetime.now(timezone.utc)

    normalized = validate_event_payload(payload)
    chain_id = normalized["chain_id"]

    chain = db.get(Chain, chain_id)
    if chain is None:
        raise IngestError(f"unknown chain: {chain_id}; register it first")

    allowed = chains_svc.active_feeder_keys(db, chain_id)
    if not allowed:
        raise IngestError(f"no active feeder registered for chain {chain_id}")
    if feeder_public_key not in allowed:
        raise IngestError("feeder public key is not registered/active for this chain")
    if not verify_signature(feeder_public_key, payload, signature):
        raise IngestError("invalid Ed25519 signature over canonical event payload")

    payload_json = canonical_json(payload).decode("utf-8")
    payload_hash = sha256_hex(payload_json.encode("utf-8"))

    existing_raw = db.scalar(
        select(RawEvent).where(RawEvent.event_key == normalized["event_key"])
    )
    if existing_raw is not None:
        # 幂等：同一身份重复投递，绝不重复计数
        existing_raw.delivery_count += 1
        if existing_raw.payload_hash != payload_hash:
            existing_raw.payload_conflict = True
        db.flush()
        return {
            "status": "duplicate",
            "event_key": normalized["event_key"],
            "delivery_count": existing_raw.delivery_count,
            "payload_conflict": existing_raw.payload_conflict,
        }

    raw = RawEvent(
        event_key=normalized["event_key"],
        chain_id=chain_id,
        payload_json=payload_json,
        payload_hash=payload_hash,
        feeder_public_key=feeder_public_key,
        first_seen_at=now,
        delivery_count=1,
    )
    db.add(raw)

    event = ChainEvent(
        event_key=normalized["event_key"],
        chain_id=chain_id,
        event_type=normalized["event_type"],
        tx_hash=normalized["tx_hash"],
        log_index=normalized["log_index"],
        block_height=normalized["block_height"],
        block_hash=normalized["block_hash"],
        source_chain=normalized["source_chain"],
        original_contract=normalized["original_contract"],
        token_id=normalized["token_id"],
        source_nonce=normalized["source_nonce"],
        account=normalized["account"],
        amount=normalized["amount"],
        asset_uid=normalized["asset_uid"],
        message_id=normalized["message_id"],
        feeder_public_key=feeder_public_key,
        first_seen_at=now,
        status="PENDING",
        prev_status="PENDING",
    )
    db.add(event)
    db.flush()
    return {"status": "accepted", "event_key": normalized["event_key"], "delivery_count": 1}
