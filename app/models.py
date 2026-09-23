"""SQLAlchemy 表模型。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Chain(Base):
    __tablename__ = "chains"

    chain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    confirmation_depth: Mapped[int] = mapped_column(Integer, default=0)
    message_timeout_seconds: Mapped[int] = mapped_column(BigInteger, default=86400)
    current_tip_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    feeders: Mapped[list["Feeder"]] = relationship(
        back_populates="chain", cascade="all, delete-orphan"
    )


class Feeder(Base):
    """链的授权 feeder（公钥）。可用多把公钥，任一把验签通过即接受。"""
    __tablename__ = "feeders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chains.chain_id", ondelete="CASCADE")
    )
    public_key_hex: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(200), default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    chain: Mapped[Chain] = relationship(back_populates="feeders")
    __table_args__ = (UniqueConstraint("chain_id", "public_key_hex", name="uq_feeder_key"),)


class Block(Base):
    """已登记块头。规范块 = 可由 chains.current_tip_hash 沿 parent_hash 回溯到的块。"""
    __tablename__ = "blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chains.chain_id", ondelete="CASCADE"), index=True
    )
    block_height: Mapped[int] = mapped_column(BigInteger)
    block_hash: Mapped[str] = mapped_column(String(66))
    parent_hash: Mapped[str] = mapped_column(String(66))
    block_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("chain_id", "block_hash", name="uq_block_hash"),
        Index("ix_blocks_chain_height", "chain_id", "block_height"),
    )


class RawEvent(Base):
    """只追加的原始投递日志（审计留痕），同事件重复投递只记一条。"""
    __tablename__ = "raw_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    chain_id: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    feeder_public_key: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivery_count: Mapped[int] = mapped_column(Integer, default=1)
    # 同一事件身份再次投递但载荷不同（equivocation）
    payload_conflict: Mapped[bool] = mapped_column(Boolean, default=False)


class ChainEvent(Base):
    """规范化事件（每事件身份一行），随对账刷新确认状态。"""
    __tablename__ = "chain_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    chain_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    tx_hash: Mapped[str] = mapped_column(String(66))
    log_index: Mapped[int] = mapped_column(Integer)
    block_height: Mapped[int] = mapped_column(BigInteger)
    block_hash: Mapped[str] = mapped_column(String(66))
    source_chain: Mapped[str] = mapped_column(String(64))
    original_contract: Mapped[str] = mapped_column(String(128))
    token_id: Mapped[str] = mapped_column(String(128), default="")
    source_nonce: Mapped[str] = mapped_column(String(128))
    account: Mapped[str] = mapped_column(String(128))
    amount: Mapped[int] = mapped_column(BigInteger)
    asset_uid: Mapped[str] = mapped_column(String(64), index=True)
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    feeder_public_key: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # PENDING / CONFIRMED / REORGED；上一次对账观察值用于检测状态翻转
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    prev_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    # 当前所认的规范块哈希；为空表示块头未登记/不可达
    canonical_block_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)


class Reconciliation(Base):
    """一次对账运行的快照头。完整快照 JSON 落盘并哈希链。"""
    __tablename__ = "reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prev_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    snapshot_path: Mapped[str] = mapped_column(Text, default="")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")


class Pairing(Base):
    """跨链消息配对（LOCK-MINT / BURN-RELEASE），每次对账全量重算。"""
    __tablename__ = "pairings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )  # noqa
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    asset_uid: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(16))  # LOCK_MINT / BURN_RELEASE
    state: Mapped[str] = mapped_column(String(24))      # PAIRED / HALF_OPEN / TIMEOUT
    lock_event_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mint_event_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    burn_event_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release_event_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lock_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mint_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    burn_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    release_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Finding(Base):
    """对账发现的问题及证据路径。只报告，不自动修复。"""
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )  # noqa
    code: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="high")
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    asset_uid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    evidence_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_finding_recon_code", "reconciliation_id", "code"),)
