"""FastAPI HTTP surface — pure JSON backend, no UI."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .eviction import audit_snapshot
from .executor import (
    ExecutorError,
    advance,
    autorun,
    evaluate,
    request_cancel,
)
from .manager import Manager
from .models import Snapshot

router = APIRouter()
manager = Manager()


# --------------------------------------------------------------------------- #
# Request / response bodies
# --------------------------------------------------------------------------- #
class CreateSimRequest(BaseModel):
    snapshot: Snapshot


class TickRequest(BaseModel):
    steps: int = Field(default=1, ge=1, le=1000)


class PodReadyRequest(BaseModel):
    namespace: str = "default"
    name: str
    ready: bool


class AddPodRequest(BaseModel):
    pod: dict
    ready_delay: Optional[int] = Field(default=None, alias="readyDelay", ge=0)


class CreateDrainRequest(BaseModel):
    nodes: list[str] = Field(min_length=1)
    ignore_daemon_sets: bool = Field(default=True, alias="ignoreDaemonSets")
    uncordon_on_complete: bool = Field(default=False, alias="uncordonOnComplete")


class AdvanceRequest(BaseModel):
    tick_wait: int = Field(default=0, alias="tickWait", ge=0, le=1000)


class AutoRunRequest(BaseModel):
    max_ticks: int = Field(default=60, alias="maxTicks", ge=1, le=2000)


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
def _error(exc: ExecutorError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


def _drain_body(drain, token: str, include_plan: bool = True) -> dict:
    plan = drain.plan.model_dump(by_alias=True, mode="json")
    body = {
        "drainId": drain.drain_id,
        "state": drain.state.value,
        "generation": drain.plan.generation,
        "snapshotGeneration": drain.sim.snap.generation,
        "tick": drain.sim.snap.tick,
        "stepIndex": drain.step_index(),
        "currentStep": (
            step.model_dump(by_alias=True, mode="json")
            if (step := drain.current_step()) is not None else None
        ),
        "preconditions": [
            p.model_dump(by_alias=True) for p in evaluate(drain)
        ],
        "blockers": [b.model_dump(by_alias=True) for b in drain.plan.blockers],
        "events": [e.model_dump(by_alias=True) for e in drain.events],
        "token": token,
    }
    if include_plan:
        body["plan"] = plan
    return body


# --------------------------------------------------------------------------- #
# Health & simulations
# --------------------------------------------------------------------------- #
@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.post("/simulations", status_code=201)
def create_simulation(req: CreateSimRequest) -> dict:
    sim_id = manager.create_simulation(req.snapshot)
    sim = manager.require_sim(sim_id)
    return {
        "simulationId": sim_id,
        "generation": sim.generation,
        "tick": sim.snap.tick,
        "podCount": len(sim.snap.pods),
        "nodeCount": len(sim.snap.nodes),
        "etag": manager.snapshot_etag(sim_id),
    }


@router.get("/simulations/{sim_id}/snapshot")
def get_snapshot(sim_id: str):
    sim = manager.require_sim(sim_id)
    data = sim.public_snapshot().model_dump(by_alias=True, mode="json")
    return JSONResponse(content=data, headers={"ETag": manager.snapshot_etag(sim_id)})


@router.post("/simulations/{sim_id}/tick")
def tick(sim_id: str, req: TickRequest) -> dict:
    sim = manager.require_sim(sim_id)
    changed = sim.tick(req.steps)
    return {
        "changed": changed,
        "generation": sim.generation,
        "tick": sim.snap.tick,
        "etag": manager.snapshot_etag(sim_id),
    }


@router.post("/simulations/{sim_id}/pods/ready")
def external_ready(sim_id: str, req: PodReadyRequest) -> dict:
    sim = manager.require_sim(sim_id)
    sim.external_set_pod_ready(req.namespace, req.name, req.ready)
    return {"generation": sim.generation, "tick": sim.snap.tick}


@router.post("/simulations/{sim_id}/pods", status_code=201)
def external_add_pod(sim_id: str, req: AddPodRequest) -> dict:
    sim = manager.require_sim(sim_id)
    pod = sim.external_add_pod(req.pod, ready_delay=req.ready_delay)
    return {
        "pod": pod.model_dump(by_alias=True, mode="json"),
        "generation": sim.generation,
    }


@router.get("/simulations/{sim_id}/audit")
def sim_audit(sim_id: str) -> dict:
    sim = manager.require_sim(sim_id)
    violations = audit_snapshot(sim.snap)
    return {
        "generation": sim.generation,
        "violations": [
            {"pdb": v.pdb, "detail": v.detail} for v in violations
        ],
    }


# --------------------------------------------------------------------------- #
# Drains
# --------------------------------------------------------------------------- #
@router.post("/simulations/{sim_id}/drains", status_code=201)
def create_drain_route(
    sim_id: str,
    req: CreateDrainRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    manager.require_sim(sim_id)
    drain, token, cached = manager.create_drain(
        sim_id,
        nodes=req.nodes,
        ignore_daemon_sets=req.ignore_daemon_sets,
        uncordon=req.uncordon_on_complete,
        idempotency_key=idempotency_key,
    )
    body = _drain_body(drain, token)
    body["idempotentReplay"] = cached
    return JSONResponse(status_code=200 if cached else 201, content=body,
                        headers={"X-Plan-Token": token})


@router.get("/simulations/{sim_id}/drains/{drain_id}")
def get_drain(sim_id: str, drain_id: str):
    drain = manager.require_drain(sim_id, drain_id)
    token = drain.token()
    return JSONResponse(
        content=_drain_body(drain, token),
        headers={"X-Plan-Token": token},
    )


@router.post("/simulations/{sim_id}/drains/{drain_id}/advance")
def advance_drain(sim_id: str, drain_id: str, req: AdvanceRequest,
                  x_plan_token: Optional[str] = Header(default=None, alias="X-Plan-Token")):
    drain = manager.require_drain(sim_id, drain_id)
    if not x_plan_token:
        raise ExecutorError("missing X-Plan-Token header", code="missing_token", status=401)
    result = advance(drain, x_plan_token, tick_wait=req.tick_wait)
    return {
        "executed": result.executed,
        "state": result.state,
        "generation": drain.plan.generation,
        "snapshotGeneration": drain.sim.snap.generation,
        "stepIndex": result.step_index,
        "currentStep": result.current_step,
        "preconditions": [
            p.model_dump(by_alias=True) for p in result.preconditions
        ],
        "replanned": result.replanned,
        "message": result.message,
        "token": result.token,
        "events": [e.model_dump(by_alias=True) for e in result.events],
    }


@router.post("/simulations/{sim_id}/drains/{drain_id}/autorun")
def autorun_drain(sim_id: str, drain_id: str, req: AutoRunRequest,
                  x_plan_token: Optional[str] = Header(default=None, alias="X-Plan-Token")):
    drain = manager.require_drain(sim_id, drain_id)
    if not x_plan_token:
        raise ExecutorError("missing X-Plan-Token header", code="missing_token", status=401)
    return autorun(drain, x_plan_token, max_ticks=req.max_ticks)


@router.post("/simulations/{sim_id}/drains/{drain_id}/cancel")
def cancel_drain(sim_id: str, drain_id: str,
                 x_plan_token: Optional[str] = Header(default=None, alias="X-Plan-Token")):
    drain = manager.require_drain(sim_id, drain_id)
    if not x_plan_token:
        raise ExecutorError("missing X-Plan-Token header", code="missing_token", status=401)
    request_cancel(drain, x_plan_token)
    return _drain_body(drain, drain.token())
