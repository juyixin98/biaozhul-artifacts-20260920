"""FastAPI 应用：激活重计算调度服务。

端点
----
- ``GET  /health``                 健康检查与版本；
- ``POST /api/v1/plans/run``       预算内选方案并执行 + 梯度校验；
- ``POST /api/v1/plans/exhaustive`` 穷举全部合法方案及画像（小图）；
- ``GET  /api/v1/runs/{run_id}``   按运行编号取回可重放的日志记录。

错误映射（``error_category`` 稳定）：

================================ =============
类别                              HTTP 状态码
================================ =============
input_error                       422
invalid_plan                      422
state_conflict                    409
resource_exhausted                507
compute_failure                   500
================================ =============
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .config import DEFAULT_CONFIG
from .errors import RecompError
from .journal import Journal
from .schemas import (
    ErrorEnvelope,
    ExhaustiveRequest,
    ExhaustiveResponse,
    PlanRequest,
    PlanResponse,
)
from .service import enumerate_plans, graph_fingerprint, plan_and_run

_STATUS_BY_CATEGORY = {
    "input_error": 422,
    "invalid_plan": 422,
    "state_conflict": 409,
    "resource_exhausted": 507,
    "compute_failure": 500,
}


def create_app(journal: Journal | None = None) -> FastAPI:
    app = FastAPI(
        title="Activation Recomputation Scheduler",
        version=__version__,
        description=(
            "为线性/带分支前向计算图规划激活检查点，并在反向中通过 RNG 快照 "
            "重放完成重计算。内存以 float64 元素数记账，全部数据为本地合成夹具。"
        ),
    )
    app.state.journal = journal or Journal(DEFAULT_CONFIG.journal_dir)

    @app.exception_handler(RecompError)
    async def _handle_recomp_error(_request, exc: RecompError):
        status = _STATUS_BY_CATEGORY[exc.category]
        run_id = app.state.journal.record_error(
            exc, context={"http_status": status}
        )
        envelope = ErrorEnvelope(
            error_category=exc.category,
            message=exc.message,
            details=exc.details,
            run_id=run_id,
        )
        return JSONResponse(status_code=status, content=envelope.model_dump())

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.post("/api/v1/plans/run", response_model=PlanResponse)
    async def run(req: PlanRequest) -> PlanResponse:
        raw = [n.model_dump() for n in req.nodes]
        summary = plan_and_run(
            raw,
            outputs=req.outputs,
            budget_elements=req.budget_elements,
            seed=req.seed,
            verify_gradients=req.verify_gradients,
            run_finite_difference=req.finite_difference,
            journal=app.state.journal,
        )
        g = summary.graph
        prof = summary.profile
        return PlanResponse(
            run_id=summary.run_id,
            graph_fingerprint=graph_fingerprint(raw, req.outputs),
            budget_elements=req.budget_elements,
            plan=summary.plan.to_dict(g),
            profile=prof.summary(),
            runtime={
                "predicted_peak": summary.result.predicted_peak,
                "runtime_peak": summary.result.runtime_peak,
                "peak_match": summary.result.peak_match(),
                "effects": {
                    "emitted": summary.result.effects_emitted,
                    "replay_verified": summary.result.effects_replay_verified,
                },
                "snapshot_balance": summary.result.rng_snapshot_balance,
            },
            gradient_check=summary.grad_check,
            candidates={
                "evaluated": summary.candidates_evaluated,
                "legal": summary.candidates_legal,
                "rejected_by_budget": summary.rejected_by_budget,
            },
            reason=summary.reason,
        )

    @app.post("/api/v1/plans/exhaustive", response_model=ExhaustiveResponse)
    async def exhaustive(req: ExhaustiveRequest) -> ExhaustiveResponse:
        raw = [n.model_dump() for n in req.nodes]
        packed = enumerate_plans(raw, outputs=req.outputs)
        fp = graph_fingerprint(raw, req.outputs)
        return ExhaustiveResponse(
            graph_fingerprint=fp,
            legal_candidate_count=packed["legal_candidate_count"],
            candidates=packed["candidates"],
            baseline=packed["baseline"],
            min_peak=packed["min_peak"],
            min_recompute_at_min_peak=packed["min_recompute_at_min_peak"],
        )

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            return app.state.journal.load(run_id)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error_category": "input_error",
                    "message": f"unknown run_id: {run_id}",
                    "details": {"run_id": run_id},
                },
            )

    return app


app = create_app()
