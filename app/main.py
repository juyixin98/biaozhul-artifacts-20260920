"""FastAPI application: incremental weighted-grid path repair service.

Endpoints
---------
POST /maps                     create a map (returns snapshot id + HMAC key)
GET  /maps/{map_id}            inspect session state
POST /maps/{map_id}/plan       plan against a named snapshot
POST /maps/{map_id}/costs      sparse cell-cost/obstacle updates
POST /maps/{map_id}/move-start relocate the start
POST /maps/{map_id}/verify     verify a response HMAC signature
DELETE /maps/{map_id}          drop a session

Every planning response carries the D* Lite cost *and* the optimal cost
computed by an independent stateless Dijkstra, plus ``optimal_match`` and
the re-expansion counts as diagnostics.
"""

from __future__ import annotations

import json

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import crypto
from .models import (
    CreateMapRequest,
    MoveStartRequest,
    PlanRequest,
    SimpleAck,
    UpdateCostsRequest,
    VerifySignatureRequest,
)
from .store import (
    InvalidMapState,
    MapStore,
    NotFound,
    SnapshotMismatch,
)

app = FastAPI(
    title="Incremental Path Repair Service",
    description="D* Lite on a weighted 2-D grid, snapshot-bound and checked "
    "against an independent Dijkstra on every plan.",
    version="1.0.0",
)
store = MapStore()


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _canonical_body(doc: dict) -> bytes:
    """Canonical JSON of a response doc (signature field excluded)."""
    doc = {k: v for k, v in doc.items() if k != "signature"}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _add_signature(session, doc: dict) -> dict:
    body = _canonical_body(doc)
    doc["signature"] = crypto.sign_response(session.hmac_key, body, doc["snapshot_id"])
    return doc


def _plan_doc(session, *, map_changed: bool) -> dict:
    outcome = session.plan()
    return _outcome_doc(session, outcome, map_changed=map_changed)


def _outcome_doc(session, outcome, *, map_changed: bool) -> dict:
    return {
        "map_id": session.map_id,
        "revision": session.revision,
        "snapshot_id": session.snapshot_id,
        "start": list(session.start),
        "goal": list(session.goal),
        "connectivity": session.connectivity,
        "reachable": outcome.reachable,
        "cost": outcome.cost if outcome.reachable else None,
        "path": [list(p) for p in outcome.path] if outcome.path is not None else None,
        "path_cost_check": outcome.path_cost_check,
        "path_valid": outcome.path_valid,
        "dijkstra_cost": outcome.dijkstra_cost if np.isfinite(outcome.dijkstra_cost) else None,
        "optimal_match": outcome.optimal_match,
        "reexpanded_nodes": outcome.reexpanded_nodes,
        "vertex_updates": outcome.vertex_updates,
        "dijkstra_settled_nodes": outcome.dijkstra_settled_nodes,
        "open_queue_size": outcome.open_queue_size,
        "km": outcome.km,
        "map_changed": map_changed,
    }


def _get_session(map_id: str):
    try:
        return store.get(map_id)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"map {map_id} not found")


# ---------------------------------------------------------------------- #
# Error mapping
# ---------------------------------------------------------------------- #
@app.exception_handler(SnapshotMismatch)
async def _snapshot_handler(_request, exc: SnapshotMismatch):
    return JSONResponse(status_code=409, content={"error": "snapshot_mismatch", "detail": str(exc)})


@app.exception_handler(InvalidMapState)
async def _invalid_handler(_request, exc: InvalidMapState):
    return JSONResponse(status_code=422, content={"error": "invalid_map_state", "detail": str(exc)})


@app.exception_handler(ValueError)
async def _value_handler(_request, exc: ValueError):
    # Planner cost validation etc.
    return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": str(exc)})


# ---------------------------------------------------------------------- #
# Endpoints
# ---------------------------------------------------------------------- #
@app.post("/maps", status_code=201)
async def create_map(req: CreateMapRequest):
    try:
        session = store.create(
            width=req.width,
            height=req.height,
            cost=np.asarray(req.cost, dtype=np.float64) if req.cost is not None else None,
            blocked=np.asarray(req.blocked, dtype=bool) if req.blocked is not None else None,
            start=tuple(req.start),
            goal=tuple(req.goal),
            connectivity=req.connectivity,
            diagonal_rule=req.diagonal_rule,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "map_id": session.map_id,
        "revision": session.revision,
        "snapshot_id": session.snapshot_id,
        "width": session.width,
        "height": session.height,
        "start": list(session.start),
        "goal": list(session.goal),
        "connectivity": session.connectivity,
        "diagonal_rule": session.diagonal_rule,
        # revealed once: clients use it to verify HMAC signatures
        "hmac_key": crypto.key_hex(session.hmac_key),
    }


@app.get("/maps/{map_id}")
async def get_map(map_id: str):
    s = _get_session(map_id)
    with s.lock:
        return {
            "map_id": s.map_id,
            "revision": s.revision,
            "snapshot_id": s.snapshot_id,
            "width": s.width,
            "height": s.height,
            "start": list(s.start),
            "goal": list(s.goal),
            "connectivity": s.connectivity,
            "diagonal_rule": s.diagonal_rule,
        }


@app.post("/maps/{map_id}/plan")
async def plan(map_id: str, req: PlanRequest):
    s = _get_session(map_id)
    outcome = s.plan(req.expected_snapshot_id)
    doc = _outcome_doc(s, outcome, map_changed=False)
    return _add_signature(s, doc)


@app.post("/maps/{map_id}/costs")
async def update_costs(map_id: str, req: UpdateCostsRequest):
    s = _get_session(map_id)
    s.update_cells(req.expected_snapshot_id, [u.model_dump() for u in req.updates])
    doc = _plan_doc(s, map_changed=True) if req.auto_replan else {
        "map_id": s.map_id,
        "revision": s.revision,
        "snapshot_id": s.snapshot_id,
        "map_changed": True,
    }
    return _add_signature(s, doc)


@app.post("/maps/{map_id}/move-start")
async def move_start(map_id: str, req: MoveStartRequest):
    s = _get_session(map_id)
    s.move_start(req.expected_snapshot_id, tuple(req.start))
    doc = _plan_doc(s, map_changed=True) if req.auto_replan else {
        "map_id": s.map_id,
        "revision": s.revision,
        "snapshot_id": s.snapshot_id,
        "map_changed": True,
    }
    return _add_signature(s, doc)


@app.post("/maps/{map_id}/verify")
async def verify(map_id: str, req: VerifySignatureRequest):
    s = _get_session(map_id)
    body = _canonical_body(req.body)
    ok = crypto.verify_signature(s.hmac_key, body, req.snapshot_id, req.signature)
    return {"map_id": map_id, "valid": ok}


@app.delete("/maps/{map_id}", response_model=SimpleAck)
async def delete_map(map_id: str):
    return SimpleAck(map_id=map_id, deleted=store.delete(map_id))


@app.get("/health")
async def health():
    return {"status": "ok"}
