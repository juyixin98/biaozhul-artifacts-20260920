"""链/feeder 登记、块头上链与规范链计算。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Block, Chain, Feeder


def register_chain(
    db: Session,
    chain_id: str,
    name: str = "",
    confirmation_depth: int = 0,
    message_timeout_seconds: int = 86400,
) -> Chain:
    chain = db.get(Chain, chain_id)
    if chain is None:
        chain = Chain(chain_id=chain_id)
        db.add(chain)
    chain.name = name or chain.name
    chain.confirmation_depth = confirmation_depth
    chain.message_timeout_seconds = message_timeout_seconds
    db.flush()
    return chain


def register_feeder(db: Session, chain_id: str, public_key_hex: str, label: str = "") -> Feeder:
    existing = db.scalar(
        select(Feeder).where(
            Feeder.chain_id == chain_id, Feeder.public_key_hex == public_key_hex
        )
    )
    if existing is not None:
        existing.revoked = False
        if label:
            existing.label = label
        return existing
    feeder = Feeder(chain_id=chain_id, public_key_hex=public_key_hex, label=label)
    db.add(feeder)
    db.flush()
    return feeder


def active_feeder_keys(db: Session, chain_id: str) -> list[str]:
    rows = db.scalars(
        select(Feeder.public_key_hex).where(
            Feeder.chain_id == chain_id, Feeder.revoked.is_(False)
        )
    ).all()
    return list(rows)


def upsert_block(
    db: Session,
    chain_id: str,
    block_height: int,
    block_hash: str,
    parent_hash: str,
    block_time: datetime,
) -> Block:
    block = db.scalar(
        select(Block).where(Block.chain_id == chain_id, Block.block_hash == block_hash)
    )
    if block is None:
        block = Block(chain_id=chain_id, block_hash=block_hash)
        db.add(block)
    block.block_height = block_height
    block.parent_hash = parent_hash
    if block_time.tzinfo is None:
        block_time = block_time.replace(tzinfo=timezone.utc)
    block.block_time = block_time.astimezone(timezone.utc)
    db.flush()
    return block


def set_tip(db: Session, chain_id: str, tip_hash: str) -> None:
    chain = db.get(Chain, chain_id)
    if chain is None:
        raise ValueError(f"unknown chain: {chain_id}")
    block = db.scalar(
        select(Block).where(Block.chain_id == chain_id, Block.block_hash == tip_hash)
    )
    if block is None:
        raise ValueError(f"tip block {tip_hash} is not registered for chain {chain_id}")
    chain.current_tip_hash = tip_hash
    db.flush()


def canonical_block_map(db: Session, chain_id: str) -> tuple[dict[str, Block], int | None]:
    """从当前 tip 沿 parent_hash 回溯，返回 {block_hash: Block} 与 tip 高度。

    任何断链（父块未登记）即停止；tip 未设置时返回空映射。
    """
    chain = db.get(Chain, chain_id)
    if chain is None or not chain.current_tip_hash:
        return {}, None
    by_hash: dict[str, Block] = {}
    cursor_hash = chain.current_tip_hash
    tip_height: int | None = None
    # 防御性上限，避免脏数据造成环导致死循环
    for _ in range(10_000_000):
        block = db.scalar(
            select(Block).where(Block.chain_id == chain_id, Block.block_hash == cursor_hash)
        )
        if block is None or block.block_hash in by_hash:
            break
        by_hash[block.block_hash] = block
        tip_height = block.block_height if tip_height is None else max(tip_height, block.block_height)
        if block.block_height == 0:
            break
        cursor_hash = block.parent_hash
    return by_hash, tip_height
