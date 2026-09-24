"""FastAPI application: append-only audit log with signed checkpoints.

Endpoints
----------
GET  /health
POST /records                  append a record (optionally checkpoint)
GET  /records                  list records, ?start=&end= inclusive (1-based)
POST /checkpoints              force a signed checkpoint now
GET  /checkpoints              list checkpoints
GET  /export                   full bundle (?start=&end= bounded slice)
GET  /anchor/public-key        demo convenience: public key PEM/hex (NOT an
                               out-of-band channel - see README)
POST /verify                   stateless reference verification

A background task signs a checkpoint every AUDIT_CHECKPOINT_INTERVAL seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from . import keys as keymod
from . import verifier as vermod
from .config import ensure_signing_key, settings
from .schemas import AppendRequest, VerifyRequest
from .store import AuditStore

logger = logging.getLogger("auditlog")

_store: AuditStore | None = None
_checkpoint_task: asyncio.Task | None = None


def get_store() -> AuditStore:
    if _store is None:
        raise RuntimeError("store not initialized")
    return _store


# --------------------------------------------------------------- background
async def _checkpoint_loop(interval: float) -> None:
    store = get_store()
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(store.create_checkpoint)
        except Exception:  # pragma: no cover - defensive
            logger.exception("periodic checkpoint failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _store, _checkpoint_task
    signing_key = await asyncio.to_thread(ensure_signing_key, settings.signing_key_path)
    _store = await asyncio.to_thread(AuditStore, settings.data_dir, signing_key)
    # An anchor over the empty log makes "genuinely empty" provable.
    await asyncio.to_thread(_store.ensure_genesis_anchor)
    _checkpoint_task = asyncio.create_task(
        _checkpoint_loop(settings.checkpoint_interval_seconds)
    )
    try:
        yield
    finally:
        if _checkpoint_task is not None:
            _checkpoint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _checkpoint_task


app = FastAPI(
    title="Tamper-evident audit log",
    version="1.0.0",
    description="Hash-chained records, Ed25519-signed checkpoints, independent verification.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- endpoints
@app.get("/health")
def health() -> dict[str, Any]:
    store = get_store()
    return {
        "status": "ok",
        "records": store.length,
        "checkpoints": len(store.checkpoints),
    }


@app.post("/records", status_code=201)
def append_record(req: AppendRequest) -> dict[str, Any]:
    store = get_store()
    record = store.append(
        actor=req.actor,
        action=req.action,
        resource=req.resource,
        payload=req.payload,
    )
    cp = None
    if settings.checkpoint_on_append:
        cp = store.create_checkpoint()
    return {"record": record, "checkpoint": cp}


@app.get("/records")
def list_records(
    start: int | None = Query(None, ge=1),
    end: int | None = Query(None, ge=1),
):
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "start must be <= end")
    store = get_store()
    return {"total": store.length, "records": store.list_records(start, end)}


@app.post("/checkpoints", status_code=201)
def force_checkpoint() -> dict[str, Any]:
    store = get_store()
    return {"checkpoint": store.create_checkpoint(force=True)}


@app.get("/checkpoints")
def list_checkpoints() -> dict[str, Any]:
    store = get_store()
    return {"checkpoints": [dict(c) for c in store.checkpoints]}


@app.get("/export")
def export(
    start: int | None = Query(None, ge=1),
    end: int | None = Query(None, ge=1),
):
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "start must be <= end")
    store = get_store()
    return store.export(start, end)


@app.get("/anchor/public-key")
def public_key() -> dict[str, str]:
    """Demo convenience ONLY. In production the verifier obtains the anchor
    out-of-band (the .public.pem file produced at key generation)."""
    store = get_store()
    pem = keymod.export_public_key_pem(store.signing_key).decode()
    return {
        "warning": (
            "Fetching the anchor from the audited server defeats the trust "
            "model; use this for local demos only."
        ),
        "public_key_hex": keymod.public_key_hex(store.signing_key),
        "public_key_pem": pem,
    }


@app.post("/verify")
def stateless_verify(req: VerifyRequest) -> dict[str, Any]:
    try:
        result = vermod.verify_bundle(
            req.bundle,
            trust_anchor_hex=req.trust_anchor,
            external_anchor=req.external_anchor,
            expect_tail=req.expect_tail,
        )
    except ValueError as exc:
        raise HTTPException(422, f"invalid trust anchor: {exc}") from exc
    return result.as_dict()
