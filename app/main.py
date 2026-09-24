"""FastAPI 入口：离线相机标定版本服务（纯后端，无前端页面）。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from . import __version__
from .calibration import CalibrationError
from .config import settings
from .crypto import payload_hash, verify_payload
from .models import CalibrationResult, VerifyResponse, VersionSummary
from .pipeline import calibrate_from_uploads
from .storage import VersionStore

app = FastAPI(
    title="相机标定版本服务",
    version=__version__,
    description="离线棋盘格相机标定：按相机与分辨率绑定不可变标定版本，"
                "逐图报告重投影误差，剔除规则全程可追溯。",
)

_store: VersionStore | None = None


def get_store() -> VersionStore:
    global _store
    if _store is None:
        _store = VersionStore(settings.storage_dir)
    return _store


@app.exception_handler(CalibrationError)
async def calibration_error_handler(_request: Request, exc: CalibrationError) -> JSONResponse:
    """业务失败统一 422 协议：{error: {code, reason, ...evidence}}，绝不伪装成功。"""
    body: dict = {"code": exc.code, "reason": exc.reason}
    body.update(exc.extra)
    return JSONResponse(status_code=422, content={"error": body})


def _strip_signature(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "signature"}


# --------------------------------------------------------------------------- #
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "camera-calibration", "version": __version__}


@app.post("/api/v1/calibrations", status_code=201, response_model=CalibrationResult)
async def create_calibration(
    camera_id: str = Form(..., min_length=1, max_length=128),
    pattern_cols: int = Form(..., ge=3, le=20),
    pattern_rows: int = Form(..., ge=3, le=20),
    square_size_mm: float = Form(..., gt=0, le=10_000),
    min_views: int | None = Form(None, ge=3, le=10_000),
    images: list[UploadFile] = File(..., min_length=1),
) -> dict:
    """上传一批棋盘格图片执行标定；成功创建不可变版本，失败 422 并附证据。"""
    raw_files: list[tuple[str, bytes]] = []
    for upload in images:
        raw_files.append((upload.filename or "unnamed", await upload.read()))

    record = calibrate_from_uploads(
        files=raw_files,
        camera_id=camera_id,
        pattern_cols=pattern_cols,
        pattern_rows=pattern_rows,
        square_size_mm=square_size_mm,
        min_views=min_views,
        storage_dir=settings.storage_dir,
    )
    get_store().save_version(record)
    return record


@app.get("/api/v1/cameras/{camera_id}/versions")
async def list_versions(
    camera_id: str,
    width: int | None = Query(None, ge=1),
    height: int | None = Query(None, ge=1),
) -> dict:
    """列出相机的标定版本；可按分辨率精确过滤。新旧版本可并行查询、互不覆盖。"""
    if (width is None) != (height is None):
        raise HTTPException(status_code=400, detail="width 与 height 必须同时提供")
    return {
        "camera_id": camera_id,
        "resolution_filter": [width, height] if width and height else None,
        "versions": [
            VersionSummary(**{**v, "resolution": list(v["resolution"])})
            for v in get_store().list_versions(camera_id, width, height)
        ],
    }


@app.get("/api/v1/versions/{version_id}", response_model=CalibrationResult)
async def get_version(version_id: str) -> dict:
    return get_store().get_version(version_id)


@app.get("/api/v1/versions/{version_id}/intrinsics")
async def get_intrinsics(version_id: str, width: int = Query(..., ge=1),
                         height: int = Query(..., ge=1)) -> dict:
    """按【精确版本 + 目标分辨率】取内参；分辨率与版本不匹配直接拒绝（防跨尺寸复用）。"""
    rec = get_store().get_version(version_id)
    bound = rec["resolution"]
    if [width, height] != list(bound):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RESOLUTION_MISMATCH",
                "reason": (f"版本 {version_id} 绑定分辨率 {bound[0]}x{bound[1]}，"
                           f"请求 {width}x{height}；标定版本不可跨分辨率复用"),
                "bound_resolution": bound,
                "requested_resolution": [width, height],
                "camera_id": rec["camera_id"],
            },
        )
    return {
        "version_id": version_id,
        "camera_id": rec["camera_id"],
        "resolution": bound,
        "intrinsics": rec["intrinsics"],
        "distortion": rec["distortion"],
        "overall_rms_px": rec["overall_rms_px"],
        "signature": rec["signature"],
    }


@app.get("/api/v1/cameras/{camera_id}/latest", response_model=CalibrationResult)
async def get_latest(camera_id: str, width: int = Query(..., ge=1),
                     height: int = Query(..., ge=1)) -> dict:
    """取相机在指定精确分辨率下的最新标定版本。"""
    return get_store().latest_version(camera_id, width, height)


@app.post("/api/v1/versions/{version_id}/verify", response_model=VerifyResponse)
async def verify_version(version_id: str) -> dict:
    """用 HMAC-SHA256 实时复算签名，检测落盘记录是否被篡改。"""
    rec = get_store().get_version(version_id)
    body = _strip_signature(rec)
    valid = verify_payload(body, rec.get("signature", ""), settings.storage_dir)
    return {
        "version_id": version_id,
        "valid": valid,
        "reason": None if valid else "签名与载荷不匹配，记录可能已被篡改",
        "payload_sha256": payload_hash(body),
    }
