"""FastAPI 服务：对显式 JSON 合约模型做有界模型检查。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .checker import BMC, MAX_BOUND, MAX_TIMEOUT_MS, MIN_TIMEOUT_MS
from .errors import ModelError, ReplayError
from .fixtures import FIXTURES
from .invariants import default_invariant
from .model import validate_model
from .replay import replay_trace

app = FastAPI(
    title="合约有界模型检查器",
    version="1.0.0",
    description="对显式 JSON 有限状态合约模型做有界模型检查（Z3）：余额非负与总额守恒。",
)


class CheckRequest(BaseModel):
    model: dict[str, Any] = Field(..., description="显式 JSON 合约模型")
    steps: int = Field(..., ge=0, le=MAX_BOUND, description="有界检查的最大步数")
    timeout_ms: int = Field(
        10_000, ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS,
        description="全部深度迭代合计的求解超时上界（毫秒）")
    use_default_invariant: bool = Field(
        True, description="为 true 时忽略 model.invariant，强制使用"
                          "「所有余额非负且总额守恒」内置不变量")


class ReplayRequest(BaseModel):
    model: dict[str, Any]
    steps: list[dict[str, Any]] = Field(
        ..., description="动作序列 [{action, params}]，从初始状态开始重放")


@app.exception_handler(ModelError)
async def model_error_handler(_request, exc: ModelError):
    return JSONResponse(status_code=422,
                        content={"error": "invalid_model", "detail": str(exc)})


@app.exception_handler(ReplayError)
async def replay_error_handler(_request, exc: ReplayError):
    return JSONResponse(status_code=409,
                        content={"error": "replay_failed", "detail": str(exc)})


@app.get("/health")
def health() -> dict:
    import z3
    return {"status": "ok", "z3_version": z3.get_version_string()}


@app.get("/fixtures")
def list_fixtures() -> dict:
    return {"fixtures": [
        {"id": k, "name": v["name"],
         "state_vars": [s["name"] for s in v["state_vars"]],
         "actions": [a["name"] for a in v["actions"]]}
        for k, v in FIXTURES.items()]}


@app.get("/fixtures/{fixture_id}")
def get_fixture(fixture_id: str) -> dict:
    if fixture_id not in FIXTURES:
        raise HTTPException(status_code=404,
                            detail=f"未知夹具 {fixture_id!r}，可用: {sorted(FIXTURES)}")
    return FIXTURES[fixture_id]


@app.post("/check")
def check(req: CheckRequest) -> dict:
    model = validate_model(req.model)
    if req.use_default_invariant:
        model["invariant"] = default_invariant(model)
    try:
        result = BMC(model).check(req.steps, req.timeout_ms)
    except ReplayError as exc:
        # 求解器反例无法被具体解释器复现：如实上报，不返回伪造反例
        raise HTTPException(status_code=500,
                            detail=f"反例重放交叉校验失败: {exc}")
    result["invariant_used"] = model["invariant"]
    return result


@app.post("/replay")
def replay(req: ReplayRequest) -> dict:
    """用具体解释器逐步重放动作序列（不调用求解器），返回每步状态与不变量判定。"""
    model = validate_model(req.model)
    inv = (model.get("invariant") or default_invariant(model))["expr"]
    trace = replay_trace(model, req.steps, inv)
    violated_at = next((t["step"] for t in trace if not t["invariant_holds"]), None)
    return {
        "trace": trace,
        "invariant_violated_at_step": violated_at,
        "invariant": {"expr": inv},
    }
