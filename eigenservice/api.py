"""FastAPI 服务入口。

运行: ``uvicorn eigenservice.api:app`` 或 ``python -m eigenservice.api``。

错误语义 (HTTP 状态 + 稳定 error_code):
- 422 invalid_matrix     形状/有限性/规模不合法
- 422 asymmetric_matrix  相对容差下不对称
- 409 not_converged      迭代预算耗尽 (不把"迭代停止"当成功, 结论单列)
- 409 quality_check_failed 收敛但残差/正交性/重构证据超阈值
- 500 internal_error     未预期错误

每个响应都带 request_id、版本、核心标识与 trace。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import CORE_IMPL, __version__
from .config import EigenConfig
from .errors import EigenserviceError
from .schemas import EigenRequest
from .service import decompose
from .trace import RequestLog

app = FastAPI(
    title="对称矩阵特征分解服务",
    version=__version__,
    description="Householder 三对角化 + 隐式 Wilkinson 移位 QL, "
                "带残差/正交性/重构证据与显式失败分类。",
)

_STATUS_BY_CODE = {
    "invalid_matrix": 422,
    "asymmetric_matrix": 422,
    "not_converged": 409,
    "quality_check_failed": 409,
}


def _override_config(req: EigenRequest, base: EigenConfig) -> EigenConfig:
    overrides = {
        key: value
        for key, value in (
            ("max_size", req.max_size),
            ("sym_tol", req.sym_tol),
            ("residual_tol", req.residual_tol),
            ("orthogonality_tol", req.orthogonality_tol),
            ("reconstruction_tol", req.reconstruction_tol),
            ("gap_tol", req.gap_tol),
            ("base_sweeps", req.base_sweeps),
            ("sweep_multiplier", req.sweep_multiplier),
        )
        if value is not None
    }
    if not overrides:
        return base
    return dataclasses.replace(base, **overrides)


@app.exception_handler(EigenserviceError)
async def eigenservice_error_handler(_: Request, exc: EigenserviceError) -> JSONResponse:
    status = _STATUS_BY_CODE.get(exc.code, 500)
    # service 层把 trace 放入 details; 提升为顶层字段并保留其余细节。
    details = dict(exc.details)
    trace = details.pop("trace", None) or {}
    return JSONResponse(
        status_code=status,
        content={
            "success": False,
            "request_id": trace.get("request_id", "-"),
            "service_version": __version__,
            "core": CORE_IMPL,
            "error_code": exc.code,
            "error_message": exc.message,
            "details": details,
            "trace": trace,
        },
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "core": CORE_IMPL}


@app.post("/eigendecompose")
async def eigendecompose(req: EigenRequest, request: Request) -> dict[str, Any]:
    request_id = req.request_id or request.headers.get("X-Request-ID")
    log = RequestLog(request_id=request_id) if request_id else RequestLog()
    config = _override_config(req, EigenConfig.from_env())
    result = decompose(req.matrix, config=config, log=log)
    return {
        "success": True,
        "request_id": log.request_id,
        "service_version": __version__,
        "core": CORE_IMPL,
        "size": result.size,
        "eigenvalues": result.eigenvalues,
        "eigenvectors": result.eigenvectors,
        "sweeps": result.sweeps,
        "quality": result.quality,
        "trace": result.trace,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
