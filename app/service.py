"""FastAPI 服务接口：/healthz、/v1/policies、/v1/sum、/v1/compare。

诊断约定：每个请求分配 request_id（或沿用 X-Request-ID），日志只记录
脱敏画像与关键状态；拒绝时响应体携带稳定的失败类别。
"""

from __future__ import annotations

import math
from typing import Iterator

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings
from app.diagnostics import RequestContext, get_logger
from app.error_analysis import build_error_report
from app.inputgen import (
    gen_cancellation,
    gen_random_spread,
    gen_small_accumulation,
    permute_chunks,
)
from app.kernels import ALL_METHODS, Method, run_method, sum_with_policy
from app.kernels.policy import FailureCategory, SummationRejected
from app.models import CompareRequest, GeneratorSpec, SumRequest

logger = get_logger("sumcompare.service")

POLICIES: dict[str, str] = {
    "empty_input": "空输入 -> 422 empty_input",
    "nan": "输入含 NaN -> 422 non_finite_input（边界拒绝，不静默传播）",
    "mixed_infinities": "同时含 +Inf 与 -Inf -> 422 mixed_infinities（不定式）",
    "single_infinity": "仅单一符号 Inf -> 200，结果为该符号 Inf",
    "signed_zero": "结果为 -0.0 当且仅当所有输入项均为 -0.0；其余精确零结果为 +0.0",
    "input_too_large": "直接提交数组超过 max_values -> 422 input_too_large（改用 generator）",
}


def _materialize(spec: GeneratorSpec) -> Iterator[float]:
    if spec.kind == "cancellation":
        if spec.n_pairs is None:
            raise SummationRejected(FailureCategory.EMPTY_INPUT, "cancellation 需要 n_pairs")
        return gen_cancellation(spec.n_pairs, spec.magnitude, spec.small)
    if spec.kind == "small_accumulation":
        if spec.count is None:
            raise SummationRejected(FailureCategory.EMPTY_INPUT, "small_accumulation 需要 count")
        return gen_small_accumulation(spec.count, spec.value)
    if spec.kind == "random_spread":
        if spec.n is None:
            raise SummationRejected(FailureCategory.EMPTY_INPUT, "random_spread 需要 n")
        return gen_random_spread(spec.n, spec.seed, spec.lo_exp, spec.hi_exp)
    raise SummationRejected(FailureCategory.EMPTY_INPUT, f"未知 generator 类型: {spec.kind!r}")


def _resolve_values(
    values: list[float] | None, spec: GeneratorSpec | None, settings: Settings
) -> list[float]:
    if values is not None:
        if len(values) > settings.max_values:
            raise SummationRejected(
                FailureCategory.INPUT_TOO_LARGE,
                f"数组长度 {len(values)} 超过上限 {settings.max_values}",
                {"count": len(values), "max_values": settings.max_values},
            )
        return [float(v) for v in values]
    assert spec is not None
    return list(_materialize(spec))


def _result_payload(result: float) -> dict:
    if math.isnan(result):
        return {"result": None, "result_repr": "nan"}
    if math.isinf(result):
        return {"result": None, "result_repr": "inf" if result > 0 else "-inf"}
    return {"result": result, "result_repr": repr(result)}


def create_app(settings: Settings | None = None) -> FastAPI:
    from app.config import get_settings

    cfg = settings or get_settings()
    app = FastAPI(title="sumcompare", version=__version__)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        ctx = RequestContext()
        incoming = request.headers.get("x-request-id")
        if incoming:
            ctx.request_id = incoming[:64]
        request.state.ctx = ctx
        response = await call_next(request)
        response.headers["X-Request-ID"] = ctx.request_id
        return response

    @app.exception_handler(SummationRejected)
    async def rejected_handler(request: Request, exc: SummationRejected) -> JSONResponse:
        ctx: RequestContext = request.state.ctx
        ctx.log_decision(logger, "rejected", category=exc.category.value, **exc.detail)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "category": exc.category.value,
                    "message": str(exc),
                    "detail": exc.detail,
                    "request_id": ctx.request_id,
                }
            },
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/policies")
    def policies() -> dict:
        return {"policies": POLICIES, "methods": [m.value for m in ALL_METHODS]}

    @app.post("/v1/sum")
    def sum_endpoint(req: SumRequest, request: Request) -> dict:
        ctx: RequestContext = request.state.ctx
        data = _resolve_values(req.values, req.generator, cfg)
        result, profile = sum_with_policy(req.method, data, req.chunk_size, cfg.pairwise_block)
        payload = _result_payload(result)
        error = None
        if req.with_error_report and payload["result"] is not None:
            error = build_error_report(req.method, result, data, cfg.reference_dps).as_dict()
        ctx.log_decision(
            logger,
            "accepted",
            method=req.method.value,
            chunk_size=req.chunk_size,
            **profile.redacted(),
        )
        return {
            "request_id": ctx.request_id,
            "method": req.method.value,
            **payload,
            "error": error,
            "diagnostics": {"input_profile": profile.redacted(), "chunk_size": req.chunk_size},
        }

    @app.post("/v1/compare")
    def compare_endpoint(req: CompareRequest, request: Request) -> dict:
        ctx: RequestContext = request.state.ctx
        data = _resolve_values(req.values, req.generator, cfg)

        methods_out: dict[str, dict] = {}
        profile = None
        for method in ALL_METHODS:
            result, profile = sum_with_policy(method, data, req.chunk_size, cfg.pairwise_block)
            payload = _result_payload(result)
            entry = dict(payload)
            if payload["result"] is not None:
                entry["error"] = build_error_report(method, result, data, cfg.reference_dps).as_dict()
            methods_out[method.value] = entry

        reorder = None
        if req.reorder_trials > 0 and profile is not None and profile.pos_inf_count == 0 and profile.neg_inf_count == 0:
            reorder = _reorder_probe(data, req.chunk_size, req.reorder_trials, req.reorder_seed)

        assert profile is not None
        ctx.log_decision(
            logger,
            "accepted",
            methods=",".join(m.value for m in ALL_METHODS),
            chunk_size=req.chunk_size,
            reorder_trials=req.reorder_trials,
            **profile.redacted(),
        )
        return {
            "request_id": ctx.request_id,
            "methods": methods_out,
            "reorder_probe": reorder,
            "diagnostics": {"input_profile": profile.redacted(), "chunk_size": req.chunk_size},
        }

    return app


def _reorder_probe(data: list[float], chunk_size: int, trials: int, seed: int) -> dict:
    """重排探针：随机排列块序，统计各方法结果的散布（scipy 描述统计）。

    展示何种重排影响何种方法：朴素法对块序敏感，补偿法应基本不动。
    """
    from scipy import stats

    n_chunks = math.ceil(len(data) / chunk_size)
    if n_chunks < 2:
        return {"note": "块数 < 2，重排无意义", "n_chunks": n_chunks}

    rng = np.random.default_rng(seed)
    per_method: dict[str, list[float]] = {m.value: [] for m in ALL_METHODS}
    for _ in range(trials):
        order = rng.permutation(n_chunks).tolist()
        shuffled = permute_chunks(data, chunk_size, order)
        for method in ALL_METHODS:
            per_method[method.value].append(run_method(method, shuffled, chunk_size))

    summary: dict[str, dict] = {}
    for name, results in per_method.items():
        arr = np.asarray(results)
        summary[name] = {
            "min": float(arr.min()),
            "max": float(arr.max()),
            "mean": float(arr.mean()),
            "std": float(arr.std()),
            "sem": float(stats.sem(arr)),  # 均值标准误：重排结果散布的统计刻画
            "distinct_results": len(set(results)),
        }
    return {"trials": trials, "n_chunks": n_chunks, "by_method": summary}
