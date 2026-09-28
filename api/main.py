"""FastAPI 服务接口：只做协议转换与错误码映射，不实现数值逻辑。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from polyroots import PolyrootError, SolveOptions, solve_polynomial
from polyroots.engine import get_default_store
from polyroots.models import CoeffOrder, KernelName

app = FastAPI(
    title="复系数多项式全部根数值求解服务",
    version="1.0.0",
    description="输入复系数多项式，返回全部根及逐根残差、近重根证据、"
                "因子重构误差与 Vieta 检查。",
)

# 单实例本地运行存储（幂等 run_id 与可重放日志）
_store = get_default_store()


class SolveRequest(BaseModel):
    # 元素可为 number | [re, im] | {"real":..,"imag":..}；pydantic 层只做容器校验，
    # 逐元素类型/有限性由数值边界 validation 模块负责并给出精确错误。
    coefficients: list[Any] = Field(..., min_length=1)
    order: CoeffOrder = CoeffOrder.DESCENDING
    kernel: KernelName = KernelName.AUTO
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    max_iterations: int = 200
    convergence_tol: float = 1e-12
    cluster_tol: float = 1e-6
    conjugate_tol: float = 1e-8
    max_degree: int = 256
    seed: int = 20260927


class HealthResponse(BaseModel):
    status: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/api/v1/roots")
def roots_endpoint(req: SolveRequest) -> dict[str, Any]:
    options = SolveOptions(
        kernel=req.kernel,
        max_iterations=req.max_iterations,
        convergence_tol=req.convergence_tol,
        cluster_tol=req.cluster_tol,
        conjugate_tol=req.conjugate_tol,
        max_degree=req.max_degree,
        seed=req.seed,
    )
    result = solve_polynomial(
        req.coefficients, order=req.order, options=options,
        run_id=req.run_id, store=_store,
    )
    payload = result.to_dict()
    payload["normalized_coeffs_asc"] = [
        [c.real, c.imag] for c in result.normalized_coeffs_asc.tolist()
    ]
    return payload


@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    record = _store.load_run(run_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={"code": "run_not_found", "category": "state_conflict",
                     "message": f"未找到 run_id={run_id} 的运行记录",
                     "details": {"run_id": run_id}},
        )
    return record


@app.exception_handler(PolyrootError)
async def polyroot_error_handler(_request: Any, exc: PolyrootError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Any, exc: Exception) -> JSONResponse:
    # 未预期错误不泄露内部细节给客户端，但保留类型名用于排查
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "category": "computation_failed",
                 "message": "服务内部错误",
                 "details": {"error_type": type(exc).__name__}},
    )
