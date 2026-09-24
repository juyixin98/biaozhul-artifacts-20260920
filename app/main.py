"""FastAPI 入口：健康检查、就绪检查、IMU 静止零偏估计（可选 HMAC 签名）。"""

from __future__ import annotations

import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import ValidationError as PydanticValidationError

from . import __version__
from .config import EstimateRequest, RuntimeConfig
from .crypto import compute_signature, get_secret, verify_request
from .errors import ServiceError, error_envelope
from .pipeline import run_pipeline
app = FastAPI(
    title="IMU 静止段识别与零偏估计服务",
    version=__version__,
    description=(
        "离线 IMU 静止段识别（滑窗方差 + 重力幅值）与陀螺零偏稳健估计。"
        "纯后端，输入加速度/角速度/采样时间，输出候选区间、残差与置信指标。"
    ),
)


def _signed_response(request: Request, payload: dict, status: int = 200) -> Response:
    # 严格 JSON（拒绝 NaN/Inf 泄漏）；签名针对实际发送的同一字节串
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = get_secret()
    if secret is not None:
        sig = compute_signature(secret, body)
        headers["X-Response-Signature"] = f"sha256={sig}"
    return Response(content=body, status_code=status, headers=headers)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "imu-bias-estimator", "version": __version__}


@app.get("/ready")
async def ready() -> dict:
    return {
        "ok": True,
        "hmac_auth_enabled": get_secret() is not None,
        "time": time.time(),
    }


@app.post("/estimate")
async def estimate(request: Request):
    raw_body = await request.body()

    secret = get_secret()
    if secret is not None:
        verify_request(
            method=request.method,
            path=request.url.path,
            body=raw_body,
            timestamp_header=request.headers.get("X-Timestamp"),
            signature_header=request.headers.get("X-Signature"),
            secret=secret,
        )

    try:
        obj = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(  # handled below
            "请求体不是合法 UTF-8 JSON", details={"detail": str(exc)}
        ) from exc

    try:
        req = EstimateRequest.model_validate(obj)
    except PydanticValidationError as exc:
        # 不回显 input：原始输入可能含 NaN/Inf 或超大数组，回显会破坏错误信封的 JSON 合法性
        issues = exc.errors(include_url=False, include_input=False, include_context=False)
        return _signed_response(
            request,
            error_envelope("validation_error", "请求参数校验失败", {"issues": issues}),
            status=422,
        )

    cfg = RuntimeConfig.from_request(req)
    result = run_pipeline(req, cfg)
    return _signed_response(request, result, status=200)


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return _signed_response(
        request,
        error_envelope(exc.error_code, exc.message, exc.details),
        status=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    return _signed_response(
        request,
        error_envelope("internal_error", f"服务器内部错误: {type(exc).__name__}: {exc}"),
        status=500,
    )
