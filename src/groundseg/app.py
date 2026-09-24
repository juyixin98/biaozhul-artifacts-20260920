"""FastAPI 应用：点云地面分割 HTTP 接口。

路由：
  GET  /healthz                         健康检查
  GET  /api/v1/info                     默认参数与标签语义说明
  POST /api/v1/segment                  执行分割（含请求 SHA-256、可选 HMAC）
  POST /api/v1/evaluate                 预测标签 vs 人工真值的精确率/召回率
"""

from __future__ import annotations

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .crypto import SHA_ALGO, request_sha256, response_sha256, sign_response
from .metrics import evaluate
from .params import GroundSegParams
from .schemas import EvaluateRequest, PointCloudRequest
from .segment import segment_points

app = FastAPI(
    title="点云地面分割服务",
    version=__version__,
    description=(
        "带种子 RANSAC 局部平面拟合 + 法向/距离地面判定。"
        "不把最大平面自动视为地面；不可靠时返回 undecidable。"
    ),
)


def _block_dict(b) -> dict:
    return {
        "tile": list(b.tile),
        "n_points": b.n_points,
        "n_core_points": b.n_core_points,
        "n_unique": b.n_unique,
        "decided": b.decided,
        "label": b.label,
        "reason": b.reason,
        "inlier_ratio": b.inlier_ratio,
        "inlier_count": b.inlier_count,
        "tilt_deg": b.tilt_deg,
        "plane_normal": b.plane_normal,
        "plane_offset": b.plane_offset,
        "confidence": b.confidence,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "ground-segmentation", "version": __version__}


@app.get("/api/v1/info")
def info() -> dict:
    d = GroundSegParams()
    return {
        "version": __version__,
        "labels": ["ground", "non_ground", "undecidable"],
        "undecidable_reasons": [
            "not_covered", "conflicting_votes",
            "too_few_points", "insufficient_unique_points",
            "ransac_too_few_points", "ransac_degenerate", "ransac_no_model",
            "tilt_exceeds_max", "inlier_ratio_too_low",
        ],
        "defaults": d.__dict__.copy(),
    }


def _validation_errors(exc: ValidationError) -> list[dict]:
    """把 Pydantic 错误裁剪成可 JSON 序列化的结构。

    默认 errors() 会带输入值（可能是 NaN）和 ctx（含异常对象），
    都会让 JSONResponse 序列化失败，这里只保留定位与消息字段。
    """
    clean = []
    for e in exc.errors(include_url=False, include_context=False,
                        include_input=False):
        clean.append({
            "loc": [str(x) for x in e.get("loc", [])],
            "type": e.get("type", "value_error"),
            "msg": e.get("msg", ""),
        })
    return clean


@app.post("/api/v1/segment")
async def segment(request: Request):
    raw = await request.body()
    # —— 真实密码学操作 1：对原始请求字节做 SHA-256 ——
    req_hash = request_sha256(raw)

    try:
        payload = PointCloudRequest.model_validate_json(raw)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "detail": _validation_errors(exc)},
        )

    if payload.point_ids is not None and len(payload.point_ids) != len(payload.points):
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request",
                     "detail": "point_ids 长度必须等于点数"},
        )
    if payload.seed_indices is not None and payload.points:
        if max(payload.seed_indices) >= len(payload.points):
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_request",
                         "detail": "seed_indices 含越界下标"},
            )

    pts = np.asarray(payload.points, dtype=np.float64)

    kw = {}
    if payload.params is not None:
        for k, v in payload.params.model_dump(exclude_none=True).items():
            kw[k] = v
    try:
        params = GroundSegParams(**kw)
        params.validate()
    except (ValueError, TypeError) as exc:
        return JSONResponse(
            status_code=422, content={"error": "invalid_params", "detail": str(exc)}
        )

    try:
        result = segment_points(
            pts, params,
            seed_indices=payload.seed_indices,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=422, content={"error": "segmentation_failed", "detail": str(exc)}
        )

    ids = payload.point_ids or [str(i) for i in range(len(pts))]
    point_results = [
        {
            "id": ids[i],
            "global_index": i,
            "label": result.labels[i],
            "confidence": round(result.confidences[i], 6),
            "reason": result.reasons[i],
        }
        for i in range(len(pts))
    ]
    body = {
        "request_sha256": req_hash,
        "request_digest_algorithm": SHA_ALGO,
        "n_points": len(pts),
        "params_used": params.__dict__.copy(),
        "stats": result.stats,
        "blocks": [_block_dict(b) for b in result.blocks],
        "points": point_results,
    }
    # —— 真实密码学操作 2：对响应体 SHA-256 + 可选 HMAC-SHA256 签名 ——
    body["response_sha256"] = response_sha256(
        {k: v for k, v in body.items() if k != "response_sha256"}
    )
    sig = sign_response(body)
    if sig is not None:
        body["signature"] = sig
    return body


@app.post("/api/v1/evaluate")
async def evaluate_route(request: Request):
    raw = await request.body()
    req_hash = request_sha256(raw)
    try:
        payload = EvaluateRequest.model_validate_json(raw)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "detail": _validation_errors(exc)},
        )
    if len(payload.predicted) != len(payload.truth):
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request",
                     "detail": "predicted 与 truth 长度必须一致"},
        )
    metrics = evaluate(payload.predicted, payload.truth)
    return {"request_sha256": req_hash, "metrics": metrics}
