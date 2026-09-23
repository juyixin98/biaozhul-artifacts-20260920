"""FastAPI application: HTTP surface for the evidence aggregation service."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, status

from . import domain, service
from .config import settings
from .db import get_pool, init_pool, init_schema
from .schemas import ChainIn, SnapshotIn, ValidatorIn, VoteIn


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = init_pool()
    with pool.connection() as conn:
        init_schema(conn)
        conn.commit()
    # Finish any DETECTED/AWAITING evidence left behind by a previous crash.
    service.recover_pending()
    yield
    get_pool().close()
    # Drop the closed global so a later lifespan (tests) builds a fresh pool.
    from . import db as _db
    _db._pool = None


app = FastAPI(
    title="Equity Penalty Evidence Aggregation",
    description=(
        "Ingests signed offline votes, canonicalizes double-sign evidence and "
        "slashes validator stake exactly once against the epoch-frozen snapshot."
    ),
    version=settings.judgment_version,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "judgment_version": settings.judgment_version}


@app.post("/chains", status_code=status.HTTP_201_CREATED)
def create_chain(body: ChainIn) -> dict:
    service.register_chain(body.chain_id)
    return {"chain_id": body.chain_id}


@app.post("/validators", status_code=status.HTTP_201_CREATED)
def create_validator(body: ValidatorIn) -> dict:
    try:
        address = service.register_validator(body.chain_id, body.public_key, body.moniker)
    except domain.DomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "chain_id": body.chain_id,
        "validator_address": address,
        "moniker": body.moniker,
    }


@app.post("/snapshots", status_code=status.HTTP_201_CREATED)
def create_snapshot(body: SnapshotIn) -> dict:
    try:
        return service.put_stake_snapshot(
            body.chain_id, body.validator_address, body.epoch, body.voting_power
        )
    except domain.DomainError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/votes", status_code=status.HTTP_200_OK)
def submit_vote(body: VoteIn) -> dict:
    """Ingest one offline vote. 200 (not 201): identical resubmits are fine."""
    try:
        result = service.ingest_vote(body.model_dump())
    except domain.DomainError as exc:
        # Malformed, bad signature, cross-chain mismatch, unknown chain/validator.
        raise HTTPException(status_code=422, detail=str(exc))
    return result.as_dict()


@app.get("/evidence")
def get_evidence_list(chain_id: str | None = Query(default=None)) -> dict:
    return {"evidence": service.list_evidence(chain_id)}


@app.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: str) -> dict:
    evidence = service.get_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return evidence


@app.get("/penalties")
def get_penalties() -> dict:
    return {"penalties": service.list_penalties()}


@app.post("/recover", status_code=status.HTTP_200_OK)
def recover() -> dict:
    """Manually trigger crash-recovery reconciliation."""
    return service.recover_pending()
