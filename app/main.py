"""FastAPI 路由: 纯后端 HTTP/JSON 协议。"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

from . import __version__
from .config import settings
from .models import AlgorithmIn, AttemptIn, CalibrationIn, ParamsIn, SnapshotIn
from .service import ReproConflict, Service

app = FastAPI(
    title="robot-experiment-snapshot",
    version=__version__,
    description="机器人离线实验可复现快照服务: bag 摘要、参数、坐标标定与算法版本绑定为不可变实验。",
)
service = Service(settings)


def _not_found(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc).strip("'\""))


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.post("/admin/recover")
def admin_recover() -> dict[str, Any]:
    return service.recover_interrupted()


# ---------- 输入注册 ----------


@app.post("/bags", status_code=201)
async def upload_bag(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    try:
        return service.register_bag(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/params", status_code=201)
def create_params(body: ParamsIn) -> dict[str, Any]:
    return service.register_params(body.model_dump())


@app.post("/calibrations", status_code=201)
def create_calibration(body: CalibrationIn) -> dict[str, Any]:
    return service.register_calibration(body.model_dump())


@app.post("/algorithms", status_code=201)
def create_algorithm(body: AlgorithmIn) -> dict[str, Any]:
    return service.register_algorithm(body.model_dump())


# ---------- 不可变快照 ----------


@app.post("/snapshots", status_code=201)
def create_snapshot(body: SnapshotIn) -> dict[str, Any]:
    try:
        return service.create_snapshot(body.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc


@app.get("/snapshots")
def list_snapshots() -> list[dict[str, Any]]:
    return service.list_snapshots()


@app.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str) -> dict[str, Any]:
    try:
        return service.get_snapshot(snapshot_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


# ---------- 运行与尝试 ----------


@app.post("/snapshots/{snapshot_id}/runs", status_code=201)
def start_run(snapshot_id: str, seed: int) -> dict[str, Any]:
    try:
        return service.start_run(snapshot_id, seed)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        return service.get_run(run_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@app.post("/runs/{run_id}/attempts", status_code=202)
def submit_attempt(run_id: str, body: AttemptIn) -> dict[str, Any]:
    try:
        return service.submit_attempt(run_id, body.delay_ms, body.fault)
    except KeyError as exc:
        raise _not_found(exc) from exc


@app.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: str) -> dict[str, Any]:
    try:
        return service.get_attempt(attempt_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@app.get("/attempts/{attempt_id}/artifacts/{kind}")
def get_artifact(attempt_id: str, kind: str) -> Response:
    if kind not in ("result", "error"):
        raise HTTPException(status_code=404, detail="kind 必须是 result 或 error")
    try:
        data, digest = service.get_artifact_bytes(attempt_id, kind)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-SHA256": digest, "X-Evidence-Id": f"art-{digest}"},
    )


@app.get("/attempts/{attempt_id}/verify")
def verify_attempt(attempt_id: str) -> dict[str, Any]:
    try:
        return service.verify_attempt(attempt_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@app.post("/attempts/{attempt_id}/mark-reproducible")
def mark_reproducible(attempt_id: str) -> dict[str, Any]:
    try:
        return service.mark_reproducible(attempt_id)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ReproConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_reproducible",
                "message": "缺少输入或运行种子记录、证据缺失或重算不一致, 不能标记为可复现",
                "checks": exc.checks,
            },
        ) from exc


# ---------- 证据修复 (仅允许按哈希恢复同一内容) ----------


@app.put("/evidence/{digest}")
async def repair_evidence(digest: str, file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    try:
        return service.repair_evidence(digest, raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
