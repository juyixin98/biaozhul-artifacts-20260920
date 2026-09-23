"""API 入参/出参模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    chain: str = Field(..., description="事件所在链，如 chainA / chainB")
    tx_hash: str
    log_index: int = Field(..., ge=0)
    block_number: int = Field(..., ge=0)
    event_type: str = Field(..., description="LOCK / MINT / BURN / RELEASE")
    message_id: str = Field(..., description="跨链协议关联消息 ID")
    source_chain: str = Field(..., description="资产源链")
    origin_contract: str = Field(..., description="资产原合约地址")
    token_id: str
    amount: int = Field(..., gt=0)
    sender: Optional[str] = None
    recipient: Optional[str] = None
    dest_chain: Optional[str] = Field(None, description="LOCK/BURN 声明的对端链，用于超时判定")


class BatchIngestRequest(BaseModel):
    events: list[EventIn]


class IngestItemResult(BaseModel):
    chain: str
    tx_hash: str
    log_index: int
    status: str  # ingested / duplicate
    event_id: Optional[int] = None


class BatchIngestResponse(BaseModel):
    ingested: int
    duplicates: int
    items: list[IngestItemResult]


class ChainHeadUpdate(BaseModel):
    height: int = Field(..., ge=0)
    confirmations: Optional[int] = Field(None, ge=0, description="该链确认数，缺省沿用当前值")


class ChainHeadOut(BaseModel):
    chain: str
    height: int
    confirmations: int


class ReorgRequest(BaseModel):
    from_height: int = Field(..., ge=0, description="从该高度（含）起的事件全部作废")


class ReorgResponse(BaseModel):
    chain: str
    from_height: int
    events_reverted: int
    ledger_entries_reverted: int
    new_head: int


class ReconcileRequest(BaseModel):
    pairing_timeout_blocks: Optional[int] = Field(None, ge=0, description="覆盖默认配对超时")


class EvidenceRef(BaseModel):
    event_id: int
    chain: str
    tx_hash: str
    log_index: int
    block_number: int
    event_type: str


class AnomalyOut(BaseModel):
    id: int
    snapshot_id: int
    anomaly_type: str
    message_id: Optional[str]
    source_chain: str
    origin_contract: str
    token_id: str
    detail: str
    evidence: list[dict]
    created_at: datetime


class AssetConservation(BaseModel):
    source_chain: str
    origin_contract: str
    token_id: str
    locked: int
    released: int
    minted: int
    burned: int
    net_locked: int   # locked - released
    net_minted: int   # minted - burned
    delta: int        # net_locked - net_minted，守恒时应为 0


class SnapshotOut(BaseModel):
    id: int
    created_at: datetime
    chain_heads: dict
    totals: list[dict]
    anomaly_count: int


class SnapshotDetail(SnapshotOut):
    anomalies: list[AnomalyOut]


class ReconcileResponse(BaseModel):
    snapshot_id: int
    newly_confirmed: int
    pairs_formed: int
    anomaly_count: int
    totals: list[dict]


class LedgerEntryOut(BaseModel):
    id: int
    event_id: int
    snapshot_id: int
    chain: str
    entry_type: str
    source_chain: str
    origin_contract: str
    token_id: str
    amount: int
    message_id: str
    status: str
    created_at: datetime
