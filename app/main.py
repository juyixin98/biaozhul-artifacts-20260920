"""FastAPI 应用：约束轨迹平滑（纯后端，无前端）。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .models import SmoothRequest
from .smoother import MAX_ITER_BUDGET, MAX_POINTS, ValidationError, smooth_trajectory

app = FastAPI(
    title="Constrained Trajectory Smoothing",
    version="1.0.0",
    description=(
        "离线二维折线路径平滑：曲率变化 + 偏离原路径双重限制，矩形障碍避让；"
        "整段精确碰撞校验，失败返回原路径。"
    ),
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """422 响应只回显错误位置与消息，不回显可能含 NaN/Infinity 的输入。"""
    safe_errors = []
    for err in exc.errors():
        safe_errors.append(
            {
                "loc": [str(x) for x in err.get("loc", [])],
                "type": err.get("type", "value_error"),
                "msg": str(err.get("msg", "")),
            }
        )
    return JSONResponse(status_code=422, content={"detail": safe_errors})


@app.get("/health")
def health():
    return {"status": "ok", "service": "trajectory-smoothing", "version": "1.0.0"}


@app.get("/limits")
def limits():
    return {
        "max_points": MAX_POINTS,
        "max_iter_default": 150,
        "max_iter_hard_limit": MAX_ITER_BUDGET,
    }


@app.post("/smooth")
def smooth(req: SmoothRequest):
    try:
        result = smooth_trajectory(req.model_dump())
    except ValidationError as exc:
        # 服务层二次校验失败（与 Pydantic 层一致映射为 422）。
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return result
