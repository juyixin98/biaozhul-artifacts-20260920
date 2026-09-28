"""FastAPI 门面层。

刻意保持薄：只做 JSON <-> numpy 的转换与请求标识透传，所有判定都在
:class:`~gradbucket.training.RoundCoordinator`。每个响应都带 ``request_id``
与 ``verdict``；错误响应带稳定的 ``code``（失败类别），测试按类别断言。
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, StrictInt

from .diagnostics import Verdict, new_request_id
from .tensors import ParamSpec
from .training import RoundCoordinator


class ParamSchema(BaseModel):
    name: str
    shape: list[int]
    dtype: str = "float64"


class BeginRoundRequest(BaseModel):
    round_index: int
    params: list[ParamSchema]
    weights: dict[str, list[Any]]
    workers: list[str]
    bucket_capacity: int = 2
    known_zero_params: list[str] = Field(default_factory=list)
    learning_rate: float = 0.1
    request_id: str | None = None


class BucketSubmitRequest(BaseModel):
    generation: int
    worker_id: str
    vec: list[float]
    mask: list[bool]
    # StrictInt：JSON 的 true/false 不得被静默当作 1/0 个样本。
    sample_count: StrictInt
    request_id: str | None = None


def create_app(liveness_timeout: float | None = None) -> FastAPI:
    """应用工厂。超时默认读环境变量 ``GRADBUCKET_LIVENESS_TIMEOUT``。"""
    if liveness_timeout is None:
        liveness_timeout = float(os.environ.get("GRADBUCKET_LIVENESS_TIMEOUT", "5.0"))
    app = FastAPI(title="gradbucket", version="0.1.0")
    coordinator = RoundCoordinator(liveness_timeout=liveness_timeout)
    app.state.coordinator = coordinator

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "generation": coordinator.generation}

    @app.get("/weights")
    def get_weights() -> dict:
        """封存前任何工作者读到的都是轮初快照（世代不变）。"""
        w = coordinator.snapshot_weights()
        return {
            "generation": coordinator.generation,
            "weights": {k: v.tolist() for k, v in w.items()},
        }

    @app.get("/rounds/current")
    def current_round() -> dict:
        view = coordinator.round_view()
        if view is None:
            raise HTTPException(status_code=404, detail={"code": "NO_OPEN_ROUND"})
        return view

    @app.post("/rounds/begin")
    def begin_round(req: BeginRoundRequest) -> dict:
        params = [ParamSpec(p.name, tuple(p.shape), p.dtype) for p in req.params]
        weights = {k: np.asarray(v, dtype=np.float64) for k, v in req.weights.items()}
        rid = req.request_id or new_request_id("begin")
        try:
            view = coordinator.begin_round(
                req.round_index,
                params,
                weights,
                req.workers,
                bucket_capacity=req.bucket_capacity,
                known_zero_params=req.known_zero_params,
                request_id=rid,
            )
        except RuntimeError as exc:
            # 已有开放轮：状态冲突，而非 500。
            raise HTTPException(
                status_code=409,
                detail={"code": "ROUND_ALREADY_OPEN", "message": str(exc)},
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_ROUND_SPEC", "message": str(exc)},
            )
        return {"request_id": rid, "round_view": view,
                "learning_rate": req.learning_rate}

    @app.post("/workers/{worker_id}/heartbeat")
    def heartbeat(worker_id: str) -> dict:
        rid = new_request_id("hb")
        try:
            used_id = coordinator.heartbeat(worker_id, request_id=rid)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={"code": "UNKNOWN_WORKER", "worker_id": worker_id},
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=409, detail={"code": "NO_OPEN_ROUND", "message": str(exc)}
            )
        return {"request_id": used_id, "worker_id": worker_id, "status": "alive"}

    @app.post("/rounds/{round_index}/buckets/{bucket_index}")
    def submit_bucket(
        round_index: int, bucket_index: int, req: BucketSubmitRequest
    ) -> dict:
        rid = req.request_id or new_request_id("sub")
        verdict = coordinator.submit_bucket(
            round_index=round_index,
            generation=req.generation,
            worker_id=req.worker_id,
            bucket_index=bucket_index,
            vec=np.asarray(req.vec, dtype=np.float64),
            mask=np.asarray(req.mask, dtype=bool),
            sample_count=req.sample_count,
            request_id=rid,
        )
        return {
            "request_id": rid,
            "verdict": verdict.value,
            "code": verdict.value,
            "round_view": coordinator.round_view(),
        }

    @app.post("/rounds/{round_index}/seal")
    def seal_round(round_index: int, learning_rate: float = 0.1) -> dict:
        view = coordinator.round_view()
        if view is None or view["round_index"] != round_index:
            raise HTTPException(status_code=404, detail={"code": "ROUND_NOT_FOUND"})
        rid = new_request_id("seal")
        try:
            verdict, commit = coordinator.seal_round(
                learning_rate=learning_rate, request_id=rid
            )
        except RuntimeError as exc:
            # 并发下可能在 view 检查后被 reset：稳定 404，而非 500。
            raise HTTPException(
                status_code=404, detail={"code": "ROUND_NOT_FOUND",
                                         "message": str(exc)}
            )
        body: dict[str, Any] = {
            "request_id": rid,
            "verdict": verdict.value,
            "code": verdict.value,
            "round_view": coordinator.round_view(),
        }
        if commit is not None:
            body["commit"] = {
                "generation_before": commit.generation_before,
                "generation_after": commit.generation_after,
                "weights": {k: v.tolist() for k, v in commit.weights.items()},
                # 梯度张量不进响应；只回传归约依据（参与方/分母/指纹）。
                "bucket_bases": list(commit.bucket_bases),
            }
        return body

    @app.post("/rounds/reset")
    def reset_round() -> dict:
        try:
            coordinator.reset_rejected_round()
        except RuntimeError as exc:
            # 开放轮不能 reset：明确的状态冲突，而非 500。
            raise HTTPException(
                status_code=409, detail={"code": "ROUND_STILL_OPEN",
                                         "message": str(exc)}
            )
        return {"status": "cleared"}

    @app.get("/diagnostics")
    def get_diagnostics() -> dict:
        log = coordinator.diagnostics()
        return {
            "verdicts": log.verdicts(),
            "records": [d.to_dict() for d in log.all()],
        }

    return app


# 默认实例供 ``uvicorn gradbucket.server:app`` 使用。
app = create_app()
