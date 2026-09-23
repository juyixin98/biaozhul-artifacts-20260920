"""数据模型。

设计要点：
- raw_events 是只追加的事实表，(chain, tx_hash, log_index) 唯一，重复投递天然去重；
- 事件状态机：PENDING -> CONFIRMED -> （可能被分叉标记为） REVERTED；
- ledger_entries 为正式账，仅由 CONFIRMED 事件产生，分叉撤销时不删除而是标记 REVERTED，保留审计轨迹；
- pairs / anomalies 由每次对账（snapshot）重算，anomalies 挂在 snapshot 上永久保留。
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, DateTime, Enum, ForeignKey, Integer,
                        Numeric, String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventType(str, enum.Enum):
    LOCK = "LOCK"        # 源链锁定
    MINT = "MINT"        # 目标链铸造
    BURN = "BURN"        # 目标链销毁（赎回发起）
    RELEASE = "RELEASE"  # 源链释放


class EventStatus(str, enum.Enum):
    PENDING = "PENDING"        # 已观测，未达确认高度
    CONFIRMED = "CONFIRMED"    # 已达确认高度，入正式账
    REVERTED = "REVERTED"      # 所在链分叉回滚，作废


class EntryStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVERTED = "REVERTED"


class AnomalyType(str, enum.Enum):
    UNMATCHED_LOCK = "UNMATCHED_LOCK"            # 锁定超时未配对铸造
    UNMATCHED_BURN = "UNMATCHED_BURN"            # 销毁超时未配对释放
    MINT_WITHOUT_LOCK = "MINT_WITHOUT_LOCK"      # 无锁定铸造
    RELEASE_WITHOUT_BURN = "RELEASE_WITHOUT_BURN"
    DUPLICATE_MINT = "DUPLICATE_MINT"            # 同一关联消息重复铸造
    CONSERVATION_MISMATCH = "CONSERVATION_MISMATCH"  # 锁铸不守恒


class ChainHead(Base):
    __tablename__ = "chain_heads"

    chain: Mapped[str] = mapped_column(String(64), primary_key=True)
    height: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    confirmations: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RawEvent(Base):
    __tablename__ = "raw_events"
    __table_args__ = (
        UniqueConstraint("chain", "tx_hash", "log_index", name="uq_event_chain_tx_log"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType, native_enum=False), nullable=False)

    # 跨链协议的关联消息 ID，用于锁定<->铸造、销毁<->释放配对
    message_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # 资产标识三元组：源链 + 原合约 + tokenID，避免跨链碰撞
    source_chain: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    origin_contract: Mapped[str] = mapped_column(String(128), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)

    amount: Mapped[int] = mapped_column(Numeric(78, 0), nullable=False)
    sender: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recipient: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # LOCK/BURN 事件声明的对端链，用于超时判定；可为空
    dest_chain: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, native_enum=False), nullable=False, default=EventStatus.PENDING, index=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LedgerEntry(Base):
    """正式账：仅 CONFIRMED 事件入账；分叉撤销时标记 REVERTED 而非删除。"""
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("raw_events.id"), nullable=False, unique=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), nullable=False)

    chain: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_type: Mapped[EventType] = mapped_column(Enum(EventType, native_enum=False), nullable=False)
    source_chain: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_contract: Mapped[str] = mapped_column(String(128), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[int] = mapped_column(Numeric(78, 0), nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[EntryStatus] = mapped_column(
        Enum(EntryStatus, native_enum=False), nullable=False, default=EntryStatus.ACTIVE, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Pair(Base):
    """按关联消息配对的结果，每次对账重算。"""
    __tablename__ = "pairs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_chain: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_contract: Mapped[str] = mapped_column(String(128), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)

    lock_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"), nullable=True)
    mint_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"), nullable=True)
    burn_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"), nullable=True)
    release_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"), nullable=True)
    # LOCK_MINTED: 锁铸成对；BURN_RELEASED: 销毁释放成对
    pair_kind: Mapped[str] = mapped_column(String(32), nullable=False)


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), nullable=False, index=True)
    anomaly_type: Mapped[AnomalyType] = mapped_column(Enum(AnomalyType, native_enum=False), nullable=False, index=True)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_chain: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_contract: Mapped[str] = mapped_column(String(128), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    # 证据路径：指向原始事件的 链/交易哈希/日志序号/区块高度 列表
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Snapshot(Base):
    """每次对账的完整快照，永久保留。"""
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    chain_heads: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 每个资产的守恒视图：[{asset, locked, released, minted, burned, delta}]
    totals: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    anomaly_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
