"""FastAPI 离线参数化服务。

纯离线计算：不连接任何硬件、不读取外部数据源、不做可视化。
启动：``uvicorn speed_profile.api:app --host 127.0.0.1 --port 8000``
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import scenarios
from .model import validate_input
from .topp import parameterize

app = FastAPI(
    title="速度约束路径参数化（离线 TOPP）",
    version="1.0.0",
    description=(
        "输入弧长采样、曲率与速度 / 加速度限制，以前后向扫描生成速度包络并计算时间。"
        "仅使用请求中提供的离线数据。"
    ),
)


class ParameterizeRequest(BaseModel):
    s: list[float] = Field(..., description="弧长采样点（米），非递减，可含相邻重复点")
    kappa: list[float] = Field(..., description="各点曲率（1/米），与 s 等长")
    v_max: float | list[float] = Field(10.0, description="速度上限（米/秒），标量或逐点")
    a_min: float = Field(-10.0, description="最小纵向加速度（米/秒²），通常为负")
    a_max: float = Field(10.0, description="最大纵向加速度（米/秒²）")
    a_lat_max: Optional[float] = Field(10.0, description="横向加速度上限；null 表示不限制")
    v_start: float = Field(0.0, description="起点速度")
    v_end: float = Field(0.0, description="终点速度")


def _json_safe(value: float) -> Optional[float]:
    """NaN / ±Inf 在 JSON 中不是合法值，统一转成 null。"""
    if value is None:
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _serialize(inp, result) -> dict:
    return {
        "feasible": result.feasible,
        "infeasible_reasons": result.infeasible_reasons,
        "n_points": int(inp.s.size),
        "s": [float(x) for x in inp.s],
        "v": [float(x) for x in result.v],
        "v_cap": [_json_safe(x) for x in result.v_cap],
        "v_forward": [float(x) for x in result.v_forward],
        "v_backward": [float(x) for x in result.v_backward],
        "times": [float(x) for x in result.times],
        "dt": [_json_safe(x) for x in result.dt],
        "a_seg": [_json_safe(x) for x in result.a_seg],
        "total_time": _json_safe(result.total_time),
        "total_time_indep_check": _json_safe(result.total_time_indep_check),
        "max_v_violation": result.max_v_violation,
        "max_a_violation": result.max_a_violation,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "offline-path-parameterization"}


@app.get("/api/scenarios")
async def list_scenarios() -> dict:
    return {"scenarios": sorted(scenarios.SCENARIOS)}


@app.get("/api/scenarios/{name}")
async def get_scenario(name: str) -> dict:
    try:
        sc = scenarios.get_scenario(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _scenario_payload(name, sc)


def _scenario_payload(name: str, sc: dict) -> dict:
    description = sc.pop("description")
    payload = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in sc.items()}
    return {"name": name, "description": description, "input": payload}


@app.post("/api/parameterize")
async def parameterize_endpoint(req: ParameterizeRequest) -> dict:
    try:
        inp = validate_input(
            s=np.asarray(req.s, dtype=float),
            kappa=np.asarray(req.kappa, dtype=float),
            v_max=(
                np.asarray(req.v_max, dtype=float)
                if isinstance(req.v_max, list)
                else req.v_max
            ),
            a_min=req.a_min,
            a_max=req.a_max,
            a_lat_max=req.a_lat_max,
            v_start=req.v_start,
            v_end=req.v_end,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"输入不合法：{exc}") from exc

    result = await run_in_threadpool(parameterize, inp)
    return _serialize(inp, result)
