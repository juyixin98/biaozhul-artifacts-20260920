"""FastAPI 应用：链/feeder/块登记、事件投递、离线对账、快照与证据查询。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import chains as chains_svc
from . import reconcile as recon_svc
from .config import get_settings
from .db import create_all, get_db
from .ingest import IngestError, ingest_event
from .models import Chain, Finding, Reconciliation
from .schemas import (
    BlocksIn,
    ChainIn,
    EventResult,
    EventsBatchIn,
    FeederIn,
    FindingOut,
    HealthOut,
    ReconciliationOut,
    ReconcileIn,
)

app = FastAPI(
    title="资产锁铸守恒核对 (Asset Lock/Mint Conservation Reconciler)",
    version="1.0.0",
    description="双链 LOCK/MINT/BURN/RELEASE 离线对账：确认度、配对、守恒、证据、快照。",
)


@app.on_event("startup")
def _startup() -> None:
    create_all()


def require_admin(authorization: str | None = Header(default=None)) -> None:
    token = get_settings().admin_token
    if not token:
        return
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing admin token")


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health(db: Session = Depends(get_db)) -> HealthOut:
    try:
        db.execute(select(1))
        return HealthOut(status="ok", database="reachable")
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}")


# ---------------------------------------------------------------------------
# 链与 feeder
# ---------------------------------------------------------------------------

@app.post("/chains", status_code=201, tags=["chains"],
          dependencies=[Depends(require_admin)])
def create_chain(body: ChainIn, db: Session = Depends(get_db)) -> dict:
    chain = chains_svc.register_chain(
        db, body.chain_id, body.name, body.confirmation_depth,
        body.message_timeout_seconds,
    )
    db.commit()
    return {"chain_id": chain.chain_id, "name": chain.name,
            "confirmation_depth": chain.confirmation_depth,
            "message_timeout_seconds": chain.message_timeout_seconds}


@app.get("/chains", tags=["chains"])
def list_chains(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Chain).order_by(Chain.chain_id)).all()
    return [{
        "chain_id": c.chain_id, "name": c.name,
        "confirmation_depth": c.confirmation_depth,
        "message_timeout_seconds": c.message_timeout_seconds,
        "current_tip_hash": c.current_tip_hash,
    } for c in rows]


@app.post("/chains/{chain_id}/feeders", status_code=201, tags=["chains"],
          dependencies=[Depends(require_admin)])
def add_feeder(chain_id: str, body: FeederIn, db: Session = Depends(get_db)) -> dict:
    if db.get(Chain, chain_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown chain: {chain_id}")
    try:
        feeder = chains_svc.register_feeder(db, chain_id, body.public_key_hex, body.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"chain_id": chain_id, "public_key_hex": feeder.public_key_hex,
            "label": feeder.label, "revoked": feeder.revoked}


@app.post("/chains/{chain_id}/blocks", status_code=200, tags=["chains"],
          dependencies=[Depends(require_admin)])
def push_blocks(chain_id: str, body: BlocksIn, db: Session = Depends(get_db)) -> dict:
    if db.get(Chain, chain_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown chain: {chain_id}")
    accepted = []
    for b in body.blocks:
        blk = chains_svc.upsert_block(
            db, chain_id, b.block_height, b.block_hash, b.parent_hash, b.block_time
        )
        accepted.append({"block_height": blk.block_height, "block_hash": blk.block_hash})
    if body.tip_block_hash:
        try:
            chains_svc.set_tip(db, chain_id, body.tip_block_hash)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"chain_id": chain_id, "blocks_accepted": len(accepted),
            "tip_block_hash": body.tip_block_hash}


# ---------------------------------------------------------------------------
# 事件投递
# ---------------------------------------------------------------------------

@app.post("/events", response_model=list[EventResult], status_code=200, tags=["events"])
def post_events(body: EventsBatchIn, db: Session = Depends(get_db)) -> list[EventResult]:
    results: list[EventResult] = []
    any_accepted = False
    for i, env in enumerate(body.events):
        try:
            res = ingest_event(
                db, env.payload, env.signature_hex, env.feeder_public_key_hex
            )
            any_accepted = any_accepted or res["status"] == "accepted"
            results.append(EventResult(
                index=i, status=res["status"], event_key=res["event_key"],
                delivery_count=res.get("delivery_count"),
                payload_conflict=res.get("payload_conflict"),
            ))
        except IngestError as exc:
            results.append(EventResult(index=i, status="rejected", error=str(exc)))
        except ValueError as exc:
            results.append(EventResult(index=i, status="rejected", error=str(exc)))
    # 合法条目随批次提交；被拒条目不影响其它条目；逐条状态在响应中给出
    db.commit()
    if not body.events:
        raise HTTPException(status_code=400, detail="empty batch")
    return results


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------

@app.post("/reconciliations", response_model=ReconciliationOut, status_code=201,
          tags=["reconcile"], dependencies=[Depends(require_admin)])
def trigger_reconciliation(
    body: ReconcileIn | None = None, db: Session = Depends(get_db)
) -> ReconciliationOut:
    as_of = body.as_of if body and body.as_of else datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    try:
        result = recon_svc.run_reconciliation(db, as_of=as_of)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"reconciliation failed: {exc}")
    return ReconciliationOut(**result)


@app.get("/reconciliations", tags=["reconcile"])
def list_reconciliations(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.scalars(
        select(Reconciliation).order_by(Reconciliation.id.desc()).limit(limit)
    ).all()
    return [{
        "run_id": r.run_id,
        "as_of": r.as_of.isoformat() if r.as_of else None,
        "snapshot_hash": r.snapshot_hash,
        "prev_snapshot_hash": r.prev_snapshot_hash,
        "snapshot_path": r.snapshot_path,
        "summary": r.summary_json,
    } for r in rows]


@app.get("/reconciliations/{run_id}", tags=["reconcile"])
def get_reconciliation(run_id: str, db: Session = Depends(get_db)) -> dict:
    r = db.scalar(select(Reconciliation).where(Reconciliation.run_id == run_id))
    if r is None:
        raise HTTPException(status_code=404, detail="no such run")
    return {
        "run_id": r.run_id,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "as_of": r.as_of.isoformat() if r.as_of else None,
        "snapshot_hash": r.snapshot_hash,
        "prev_snapshot_hash": r.prev_snapshot_hash,
        "snapshot_path": r.snapshot_path,
        "summary": r.summary_json,
    }


@app.get("/findings", response_model=list[FindingOut], tags=["reconcile"])
def list_findings(
    code: str | None = None,
    severity: str | None = None,
    run_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[FindingOut]:
    q = select(Finding)
    if code:
        q = q.where(Finding.code == code)
    if severity:
        q = q.where(Finding.severity == severity)
    if run_id:
        recon = db.scalar(select(Reconciliation).where(Reconciliation.run_id == run_id))
        if recon is None:
            raise HTTPException(status_code=404, detail="no such run")
        q = q.where(Finding.reconciliation_id == recon.id)
    q = q.order_by(Finding.id.desc()).limit(limit)
    rows = db.scalars(q).all()
    return [FindingOut(
        code=f.code, severity=f.severity, message_id=f.message_id,
        asset_uid=f.asset_uid, detail=f.detail, evidence_path=f.evidence_path,
    ) for f in rows]


@app.get("/stats", tags=["meta"])
def stats(db: Session = Depends(get_db)) -> dict:
    return {
        "chains": db.scalar(select(func.count()).select_from(Chain)) or 0,
        "reconciliations": db.scalar(select(func.count()).select_from(Reconciliation)) or 0,
        "findings": db.scalar(select(func.count()).select_from(Finding)) or 0,
    }
