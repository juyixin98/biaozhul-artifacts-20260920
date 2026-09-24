"""FastAPI application: offline camera calibration service."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from . import config, storage
from .calibration import PipelineOutcome, calibrate_pipeline
from .models import (
    CalibrationRequestJson,
    CalibrationResult,
    ExclusionSummary,
    ImageReport,
    Intrinsics,
    JobReport,
    PoseModel,
    VersionDetail,
    VersionSummary,
)

app = FastAPI(title="Camera Calibration Service", version="1.0.0")


# --------------------------------------------------------------------- helpers


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _thresholds() -> dict[str, float | int]:
    return {
        "min_views": config.MIN_VIEWS,
        "outlier_max_error_px": config.OUTLIER_MAX_PX,
        "outlier_median_k": config.OUTLIER_MEDIAN_K,
        "duplicate_angle_deg": config.DUPLICATE_ANGLE_DEG,
        "duplicate_translation_ratio": config.DUPLICATE_TRANSLATION_RATIO,
        "coverage_svd_ratio": config.COVERAGE_SVD_RATIO,
        "coverage_tilt_spread_deg": config.COVERAGE_TILT_SPREAD_DEG,
        "coverage_ray_spread_deg": config.COVERAGE_RAY_SPREAD_DEG,
    }


def _build_result(outcome: PipelineOutcome) -> CalibrationResult:
    K = outcome.camera_matrix
    D = outcome.dist_coeffs
    accepted = [
        t for t in outcome.traces if t.index in set(outcome.accepted_indices)
    ]
    errs = [t.reprojection_error_px for t in accepted if t.reprojection_error_px is not None]
    intrinsics = Intrinsics(
        camera_matrix=np.round(K, 8).tolist(),
        dist_coeffs=np.round(D.reshape(-1), 8).tolist(),
        fx=float(K[0, 0]),
        fy=float(K[1, 1]),
        cx=float(K[0, 2]),
        cy=float(K[1, 2]),
    )
    return CalibrationResult(
        rms=round(float(outcome.rms), 8),
        per_view_rms_px=round(float(np.sqrt(np.mean(np.square(errs)))), 8) if errs else None,
        max_view_error_px=round(max(errs), 8) if errs else None,
        intrinsics=intrinsics,
        accepted_count=len(accepted),
        coverage=outcome.coverage,
    )


def _image_reports(outcome: PipelineOutcome) -> list[ImageReport]:
    reports: list[ImageReport] = []
    for t in outcome.traces:
        reports.append(
            ImageReport(
                index=t.index,
                filename=t.filename,
                sha256=t.sha256,
                width=t.width,
                height=t.height,
                status=t.status,
                reason=t.reason,
                detail=t.detail,
                duplicate_of=t.duplicate_of,
                reprojection_error_px=t.reprojection_error_px,
                pose=PoseModel(rvec=t.rvec, tvec=t.tvec) if t.rvec is not None else None,
            )
        )
    return reports


def _exclusion_summary(traces) -> ExclusionSummary:
    reasons = [t.reason for t in traces if t.status == "rejected"]
    return ExclusionSummary(**{k: reasons.count(k) for k in ExclusionSummary.model_fields})


def _execute(
    images: list[tuple[str, bytes]],
    camera_id: str,
    width: int,
    height: int,
    board_rows: int,
    board_cols: int,
    square_mm: float,
) -> JSONResponse:
    if not images:
        raise HTTPException(status_code=422, detail="no image files provided")
    if len(images) > config.MAX_IMAGES_PER_JOB:
        raise HTTPException(
            status_code=422,
            detail=f"too many images: {len(images)} > {config.MAX_IMAGES_PER_JOB}",
        )

    outcome = calibrate_pipeline(
        images, camera_id, width, height, board_rows, board_cols, square_mm
    )

    job_id = uuid.uuid4().hex
    job_payload: dict = {
        "job_id": job_id,
        "camera_id": camera_id,
        "width": width,
        "height": height,
        "board_rows": board_rows,
        "board_cols": board_cols,
        "square_size_mm": square_mm,
        "status": "succeeded" if outcome.success else "failed",
        "failure_reason": outcome.failure_reason,
        "failure_detail": outcome.failure_detail,
        "created_version": None,
        "version_id": None,
        "result": None,
        "images": [r.model_dump() for r in _image_reports(outcome)],
        "exclusions": _exclusion_summary(outcome.traces).model_dump(),
        "thresholds": _thresholds(),
        "created_at": _utcnow(),
    }

    version_record = None
    if outcome.success:
        result = _build_result(outcome)
        job_payload["result"] = result.model_dump()
        version_record = storage.save_version(
            {
                "camera_id": camera_id,
                "width": width,
                "height": height,
                "board_rows": board_rows,
                "board_cols": board_cols,
                "square_size_mm": square_mm,
                "created_at": _utcnow(),
                "job_id": job_id,
                "rms": result.rms,
                "accepted_count": result.accepted_count,
                "result": result.model_dump(mode="json"),
                "images": job_payload["images"],
            }
        )
        job_payload["created_version"] = version_record["version"]
        job_payload["version_id"] = version_record["version_id"]

    storage.save_job(job_payload)

    job_report = JobReport(
        **{k: v for k, v in job_payload.items() if k != "created_at"}
    )
    body = job_report.model_dump(mode="json")
    body["created_at"] = job_payload["created_at"]
    if version_record is not None:
        body["signature"] = version_record["signature"]
    return JSONResponse(status_code=201 if outcome.success else 422, content=body)


def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for f in files:
        data = b""
        while chunk := f.file.read(1 << 20):
            data += chunk
            if len(data) > config.MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=f"file {f.filename!r} exceeds upload size limit",
                )
        out.append((f.filename or f"file_{len(out)}", data))
    return out


# --------------------------------------------------------------------- routes


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/calibrations")
async def create_calibration(
    camera_id: str = Form(...),
    width: int = Form(...),
    height: int = Form(...),
    board_rows: int = Form(...),
    board_cols: int = Form(...),
    square_size_mm: float = Form(...),
    files: list[UploadFile] = File(...),
):
    # validate via the same rules as the JSON endpoint where applicable
    if not all(c.isalnum() or c in "._-" for c in camera_id) or not camera_id:
        raise HTTPException(422, "illegal camera_id")
    if not (16 <= width <= 100_000 and 16 <= height <= 100_000):
        raise HTTPException(422, "illegal resolution")
    if not (2 <= board_rows <= 100 and 2 <= board_cols <= 100) or square_size_mm <= 0:
        raise HTTPException(422, "illegal board specification")
    images = _read_uploads(files)
    return _execute(images, camera_id, width, height, board_rows, board_cols, square_size_mm)


def _resolve_under_roots(raw_path: str) -> Path:
    p = Path(raw_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        resolved = p.resolve(strict=False)
    except OSError as exc:
        raise HTTPException(422, f"bad path {raw_path!r}: {exc}")
    for root in config.IMAGE_ROOTS:
        root = root.resolve()
        if root == resolved or root in resolved.parents:
            if not resolved.is_file():
                raise HTTPException(422, f"not a file: {raw_path!r}")
            return resolved
    raise HTTPException(
        403, f"path {raw_path!r} is outside allowed image roots"
    )


@app.post("/calibrations/from-paths")
def create_from_paths(req: CalibrationRequestJson):
    images: list[tuple[str, bytes]] = []
    for raw in req.image_paths:
        resolved = _resolve_under_roots(raw)
        if resolved.stat().st_size > config.MAX_UPLOAD_BYTES:
            raise HTTPException(422, f"file too large: {raw!r}")
        images.append((resolved.name, resolved.read_bytes()))
    return _execute(
        images,
        req.camera_id,
        req.width,
        req.height,
        req.board_rows,
        req.board_cols,
        req.square_size_mm,
    )


def _resolution_conflict(camera_id: str, width: int, height: int) -> HTTPException:
    available = [list(r) for r in storage.list_resolutions(camera_id)]
    return HTTPException(
        status_code=409,
        detail={
            "detail": (
                f"camera {camera_id!r} has no calibration bound to "
                f"{width}x{height}; versions never reuse across resolutions"
            ),
            "camera_id": camera_id,
            "requested_width": width,
            "requested_height": height,
            "available_resolutions": available,
        },
    )


def _summarize(raw: dict) -> VersionSummary:
    return VersionSummary(
        version_id=raw["version_id"],
        camera_id=raw["camera_id"],
        width=raw["width"],
        height=raw["height"],
        version=raw["version"],
        created_at=raw["created_at"],
        rms=raw["rms"],
        accepted_count=raw["accepted_count"],
        job_id=raw["job_id"],
        signature=raw["signature"],
        signature_valid=raw.get("signature_valid", True),
    )


def _detail(raw: dict) -> VersionDetail:
    summary = _summarize(raw).model_dump()
    return VersionDetail(
        **summary,
        board_rows=raw["board_rows"],
        board_cols=raw["board_cols"],
        square_size_mm=raw["square_size_mm"],
        result=raw["result"],
        images=raw["images"],
    )


@app.get("/cameras/{camera_id}/calibrations")
def list_calibrations(camera_id: str, width: int, height: int):
    versions = storage.list_versions(camera_id, width, height)
    if not versions:
        raise _resolution_conflict(camera_id, width, height)
    return [_summarize(v) for v in versions]


@app.get("/cameras/{camera_id}/calibrations/latest")
def latest_calibration(camera_id: str, width: int, height: int, detail: str | None = None):
    raw = storage.get_latest(camera_id, width, height)
    if raw is None:
        raise _resolution_conflict(camera_id, width, height)
    return _detail(raw) if detail == "full" else _summarize(raw)


@app.get("/cameras/{camera_id}/calibrations/v{version}")
def get_calibration(camera_id: str, version: int, width: int, height: int):
    raw = storage.get_version(camera_id, width, height, version)
    if raw is None:
        # Resolutions with no versions at all are binding conflicts (409);
        # a missing version number at a known resolution is a plain 404.
        if not storage.list_versions(camera_id, width, height):
            raise _resolution_conflict(camera_id, width, height)
        raise HTTPException(
            404,
            f"version v{version} not found for {camera_id} @ {width}x{height}",
        )
    return _detail(raw)


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    raw = storage.load_job(job_id)
    if raw is None:
        raise HTTPException(404, "job not found")
    return raw
