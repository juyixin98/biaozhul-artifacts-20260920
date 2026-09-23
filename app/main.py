"""FastAPI 入口：资产锁铸守恒核对服务（纯后端）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .models import (Anomaly, ChainHead, EntryStatus, LedgerEntry, RawEvent,
                     Snapshot)
from .reconciler import (apply_reorg, get_heads, ingest_events, run_reconcile,
                         set_chain_head)
from .schemas import (AnomalyOut, AssetConservation, BatchIngestRequest,
                      BatchIngestResponse, ChainHeadOut, ChainHeadUpdate,
                      IngestItemResult, LedgerEntryOut, ReconcileRequest,
                      ReconcileResponse, ReorgRequest, ReorgResponse,
                      SnapshotDetail, SnapshotOut)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="资产锁铸守恒核对", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 事件摄取

@app.post("/events/batch", response_model=BatchIngestResponse)
def post_events(req: BatchIngestRequest, db: Session = Depends(get_db)):
    """批量摄取事件；按 (chain, tx_hash, log_index) 去重，重复投递不重复计数。"""
    new_events, duplicates = ingest_events(db, [e.model_dump() for e in req.events])
    db.commit()
    items = [IngestItemResult(chain=e.chain, tx_hash=e.tx_hash, log_index=e.log_index,
                              status="ingested", event_id=e.id) for e in new_events]
    items += [IngestItemResult(chain=e["chain"], tx_hash=e["tx_hash"], log_index=e["log_index"],
                               status="duplicate", event_id=None) for e in duplicates]
    return BatchIngestResponse(ingested=len(new_events), duplicates=len(duplicates), items=items)


# ---------------------------------------------------------------- 链高度与分叉

@app.get("/chains", response_model=list[ChainHeadOut])
def list_chains(db: Session = Depends(get_db)):
    return [ChainHeadOut(chain=h.chain, height=h.height, confirmations=h.confirmations)
            for h in get_heads(db).values()]


@app.put("/chains/{chain}/head", response_model=ChainHeadOut)
def put_chain_head(chain: str, req: ChainHeadUpdate, db: Session = Depends(get_db)):
    head = set_chain_head(db, chain, req.height, req.confirmations)
    db.commit()
    return ChainHeadOut(chain=head.chain, height=head.height, confirmations=head.confirmations)


@app.post("/chains/{chain}/reorg", response_model=ReorgResponse)
def post_reorg(chain: str, req: ReorgRequest, db: Session = Depends(get_db)):
    """分叉撤销：from_height（含）起的事件作废，对应账目标记 REVERTED（保留审计轨迹）。"""
    events_reverted, entries_reverted, new_head = apply_reorg(db, chain, req.from_height)
    db.commit()
    return ReorgResponse(chain=chain, from_height=req.from_height,
                         events_reverted=events_reverted,
                         ledger_entries_reverted=entries_reverted,
                         new_head=new_head)


# ---------------------------------------------------------------- 对账

@app.post("/reconcile", response_model=ReconcileResponse)
def post_reconcile(req: ReconcileRequest | None = None, db: Session = Depends(get_db)):
    """执行一次对账：推进确认、配对、产出异常与守恒视图，并保留快照。"""
    timeout = req.pairing_timeout_blocks if req else None
    snapshot, result = run_reconcile(db, timeout)
    db.commit()
    return ReconcileResponse(**result)


@app.get("/snapshots", response_model=list[SnapshotOut])
def list_snapshots(db: Session = Depends(get_db)):
    rows = db.execute(select(Snapshot).order_by(Snapshot.id)).scalars().all()
    return [SnapshotOut(id=s.id, created_at=s.created_at, chain_heads=s.chain_heads,
                        totals=s.totals, anomaly_count=s.anomaly_count) for s in rows]


@app.get("/snapshots/{snapshot_id}", response_model=SnapshotDetail)
def get_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    s = db.get(Snapshot, snapshot_id)
    if s is None:
        raise HTTPException(404, f"snapshot {snapshot_id} not found")
    anomalies = db.execute(
        select(Anomaly).where(Anomaly.snapshot_id == s.id).order_by(Anomaly.id)
    ).scalars().all()
    return SnapshotDetail(
        id=s.id, created_at=s.created_at, chain_heads=s.chain_heads,
        totals=s.totals, anomaly_count=s.anomaly_count,
        anomalies=[AnomalyOut(id=a.id, snapshot_id=a.snapshot_id,
                              anomaly_type=a.anomaly_type.value, message_id=a.message_id,
                              source_chain=a.source_chain, origin_contract=a.origin_contract,
                              token_id=a.token_id, detail=a.detail, evidence=a.evidence,
                              created_at=a.created_at) for a in anomalies],
    )


# ---------------------------------------------------------------- 查询

@app.get("/anomalies", response_model=list[AnomalyOut])
def list_anomalies(snapshot_id: int | None = Query(None),
                   anomaly_type: str | None = Query(None),
                   db: Session = Depends(get_db)):
    stmt = select(Anomaly).order_by(Anomaly.id)
    if snapshot_id is not None:
        stmt = stmt.where(Anomaly.snapshot_id == snapshot_id)
    if anomaly_type is not None:
        stmt = stmt.where(Anomaly.anomaly_type == anomaly_type)
    rows = db.execute(stmt).scalars().all()
    return [AnomalyOut(id=a.id, snapshot_id=a.snapshot_id, anomaly_type=a.anomaly_type.value,
                       message_id=a.message_id, source_chain=a.source_chain,
                       origin_contract=a.origin_contract, token_id=a.token_id,
                       detail=a.detail, evidence=a.evidence, created_at=a.created_at)
            for a in rows]


@app.get("/ledger", response_model=list[LedgerEntryOut])
def list_ledger(chain: str | None = Query(None),
                status: str | None = Query(None),
                db: Session = Depends(get_db)):
    stmt = select(LedgerEntry).order_by(LedgerEntry.id)
    if chain is not None:
        stmt = stmt.where(LedgerEntry.chain == chain)
    if status is not None:
        stmt = stmt.where(LedgerEntry.status == status)
    rows = db.execute(stmt).scalars().all()
    return [LedgerEntryOut(id=e.id, event_id=e.event_id, snapshot_id=e.snapshot_id,
                           chain=e.chain, entry_type=e.entry_type.value,
                           source_chain=e.source_chain, origin_contract=e.origin_contract,
                           token_id=e.token_id, amount=int(e.amount),
                           message_id=e.message_id, status=e.status.value,
                           created_at=e.created_at) for e in rows]


@app.get("/assets", response_model=list[AssetConservation])
def list_assets(db: Session = Depends(get_db)):
    """当前生效账目（ACTIVE）下的各资产守恒视图。"""
    rows = db.execute(
        select(LedgerEntry).where(LedgerEntry.status == EntryStatus.ACTIVE)
    ).scalars().all()
    per_asset: dict[tuple, dict] = {}
    for e in rows:
        key = (e.source_chain, e.origin_contract, e.token_id)
        slot = per_asset.setdefault(key, {"locked": 0, "released": 0, "minted": 0, "burned": 0})
        if e.entry_type.value == "LOCK":
            slot["locked"] += int(e.amount)
        elif e.entry_type.value == "RELEASE":
            slot["released"] += int(e.amount)
        elif e.entry_type.value == "MINT":
            slot["minted"] += int(e.amount)
        elif e.entry_type.value == "BURN":
            slot["burned"] += int(e.amount)
    out = []
    for (sc, contract, tid), slot in sorted(per_asset.items()):
        net_locked = slot["locked"] - slot["released"]
        net_minted = slot["minted"] - slot["burned"]
        out.append(AssetConservation(source_chain=sc, origin_contract=contract, token_id=tid,
                                     net_locked=net_locked, net_minted=net_minted,
                                     delta=net_locked - net_minted, **slot))
    return out
