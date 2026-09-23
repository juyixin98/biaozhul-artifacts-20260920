"""FastAPI 入口：快照管理、计划、执行、模拟器控制、密码学材料。

所有写接口走签名信封（Ed25519 + 一次性 nonce + 时间戳），公开只读接口除外。
"""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .crypto import canonical
from .models import SnapshotSpec
from .planner import build_plan
from .store import AuthError, Store

app = FastAPI(title="workload-drain-planner", version="1.0.0")
store = Store()


# ---------- 请求模型 ----------
# 签名信封 {kid, nonce, ts, payload, sig} 由 store.verify_envelope 统一校验，
# 缺失/伪造字段一律 401（而不是 Pydantic 422），语义统一。


class PlanRequest(BaseModel):
    snapshot: dict[str, Any]
    drain_nodes: list[str]
    force: bool = False


class ExecuteRequest(BaseModel):
    plan_id: str
    force: bool = False
    uncordon_on_cancel: bool = False
    step_timeout_ticks: int = Field(default=20, ge=1, le=10_000)


class FaultRequest(BaseModel):
    namespace: str = "default"
    deployment: str
    mode: str  # never_ready | delay_ready | clear
    delay: int | None = Field(default=None, ge=0)


class KeyRequest(BaseModel):
    public_key_pem: str


# ---------- 错误处理 ----------

@app.exception_handler(AuthError)
def auth_handler(request, exc: AuthError):
    return JSONResponse(status_code=401, content={"error": "unauthorized", "detail": str(exc)})


@app.exception_handler(KeyError)
def key_handler(request, exc: KeyError):
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(ValueError)
def value_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"error": "bad_request", "detail": str(exc)})


# ---------- 只读/引导接口 ----------

@app.get("/healthz")
def healthz():
    return {"status": "ok", "snapshot_loaded": store.snapshot_loaded}


@app.get("/api/server-key")
def server_key():
    info = store.server_identity.public_info()
    info["usage"] = "服务端对计划与响应签名；客户端可用此公钥验签"
    return info


@app.post("/api/nonces")
def create_nonce():
    return store.issue_nonce()


@app.post("/api/client-keys")
def register_client_key(req: KeyRequest):
    return store.register_client_key(req.public_key_pem)


# ---------- 快照 ----------

@app.get("/api/snapshot")
def get_snapshot():
    if not store.snapshot_loaded:
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": "尚未载入快照"})
    return store.sign_response_payload(
        {"snapshot": store.sim.export_snapshot(), "generation": store.sim.generation,
         "tick": store.sim.tick_n, "digest": store.sim.digest()}
    )


@app.post("/api/snapshot")
async def load_snapshot(env: dict):
    payload = store.verify_envelope(env)
    spec = SnapshotSpec(**payload["snapshot"])
    store.sim.load_snapshot(spec.model_dump())
    store.snapshot_loaded = True
    return store.sign_response_payload(
        {"loaded": store.sim.snapshot_id, "generation": store.sim.generation,
         "digest": store.sim.digest(), "pdb_status": store.sim.pdb_status()}
    )


@app.post("/api/plans")
async def create_plan(env: dict):
    payload = store.verify_envelope(env)
    req = PlanRequest(**payload)
    # 允许两种用法：直接带 snapshot（同时载入模拟器），或对当前已载入快照规划
    if req.snapshot:
        spec = SnapshotSpec(**req.snapshot)
        store.sim.load_snapshot(spec.model_dump())
        store.snapshot_loaded = True
        snap = store.sim.export_snapshot()
    else:
        if not store.snapshot_loaded:
            raise ValueError("尚未载入快照：请在请求中提供 snapshot，或先 POST /api/snapshot")
        snap = store.sim.export_snapshot()
    plan = build_plan(snap, req.drain_nodes, force=req.force)
    plan_id = "plan-" + hashlib.sha256(
        canonical({"digest": plan["snapshot_digest"], "nodes": plan["drain_nodes"], "force": plan["force"]})
    ).hexdigest()[:16]
    plan["plan_id"] = plan_id
    plan["server_signature"] = store.server_identity.sign_object(
        {k: v for k, v in plan.items() if k != "server_signature"}
    )
    store.plans[plan_id] = plan
    return store.sign_response_payload(plan)


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: str):
    if plan_id not in store.plans:
        raise KeyError(f"plan {plan_id} 不存在")
    return store.sign_response_payload(store.plans[plan_id])


# ---------- 执行 ----------

@app.post("/api/executions")
async def create_execution(env: dict):
    payload = store.verify_envelope(env)
    req = ExecuteRequest(**payload)
    if req.plan_id not in store.plans:
        raise KeyError(f"plan {req.plan_id} 不存在")
    plan = store.plans[req.plan_id]
    ex = store.executor.create_execution(
        plan,
        {
            "force": req.force or plan.get("force", False),
            "uncordon_on_cancel": req.uncordon_on_cancel,
            "step_timeout_ticks": req.step_timeout_ticks,
        },
    )
    return store.sign_response_payload(store.executor.describe(ex))


@app.get("/api/executions/{exec_id}")
def get_execution(exec_id: str):
    ex = store.executor.get(exec_id)
    return store.sign_response_payload(store.executor.describe(ex))


@app.post("/api/executions/{exec_id}/advance")
def advance_execution(exec_id: str, env: dict):
    store.verify_envelope(env)
    ex = store.executor.get(exec_id)
    ex = store.executor.advance(exec_id)
    return store.sign_response_payload(store.executor.describe(ex))


@app.post("/api/executions/{exec_id}/cancel")
def cancel_execution(exec_id: str, env: dict):
    store.verify_envelope(env)
    ex = store.executor.cancel(exec_id)
    return store.sign_response_payload(store.executor.describe(ex))


@app.post("/api/executions/{exec_id}/resume")
def resume_execution(exec_id: str, env: dict):
    store.verify_envelope(env)
    ex = store.executor.resume(exec_id)
    return store.sign_response_payload(store.executor.describe(ex))


# ---------- 模拟器控制 ----------

@app.post("/api/sim/tick")
def sim_tick(env: dict):
    store.verify_envelope(env)
    result = store.sim.tick()
    return store.sign_response_payload(
        {"generation": store.sim.generation, **result, "pdb_status": store.sim.pdb_status()}
    )


@app.post("/api/sim/fault")
def sim_fault(env: dict):
    payload = store.verify_envelope(env)
    req = FaultRequest(**payload)
    if req.mode == "clear":
        store.sim.clear_fault(req.namespace, req.deployment)
    else:
        store.sim.inject_fault(req.namespace, req.deployment, req.mode, req.delay)
    return store.sign_response_payload(
        {"fault": payload, "generation": store.sim.generation,
         "fault_generation": store.sim.fault_generation,
         "pdb_status": store.sim.pdb_status()}
    )


@app.get("/api/events")
def get_events(after: int = 0):
    return store.sign_response_payload({"events": store.sim.get_events(after)})
