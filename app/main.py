"""FastAPI application exposing the ground-segmentation pipeline.

Run locally::

    uvicorn app.main:app --host 127.0.0.1 --port 8000

The service is stateless; every request carries all points and parameters.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .models import ChunkOptions, RansacOptions, SegmentRequest
from .segmentation.chunking import (
    ChunkConfig,
    SegmentationOutput,
    segment_cloud,
)
from .segmentation.ransac import RansacConfig

app = FastAPI(
    title="Point-Cloud Ground Segmentation",
    version="1.0.0",
    description=(
        "Seeded RANSAC local-plane fitting with tilt/inlier reliability gates, "
        "spatial chunking and confidence-based overlap merging."
    ),
)


def _request_hash(req: SegmentRequest) -> str:
    """SHA-256 over the canonical point list — a real checksum of the payload."""
    payload = json.dumps(
        [[p.id, p.x, p.y, p.z] for p in req.points],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _to_configs(req: SegmentRequest) -> tuple[RansacConfig, ChunkConfig]:
    ro = req.ransac.model_dump() if req.ransac else {}
    co = req.chunk.model_dump() if req.chunk else {}
    # Cross-field coherence: a chunk must not demand more points than RANSAC.
    if co and ro and co["min_points"] < ro["min_points"]:
        raise HTTPException(
            status_code=422,
            detail="chunk.min_points must be >= ransac.min_points "
                   "(smaller chunks are silently skipped; keep the gates coherent)",
        )
    return RansacConfig(**ro), ChunkConfig(**co)


def _serialize(out: SegmentationOutput, ids: list[int],
               sha: str) -> dict:
    results = []
    for pid, label, conf, info in zip(
        out.point_ids, out.labels, out.confidences, out.merge_info
    ):
        results.append({
            "id": ids[pid],
            "label": label,
            "confidence": conf,
            "vote_count": int(info.get("vote_count", 0)),
            "support_ground": round(float(info.get("support_ground", 0.0)), 6),
            "support_non_ground": round(
                float(info.get("support_non_ground", 0.0)), 6
            ),
            "chunks": info.get("chunks", []),
        })
    chunk_reports = [
        {
            "chunk_id": r.chunk_id,
            "point_count": r.point_count,
            "status": r.status,
            "reason": r.reason,
            "inlier_count": r.inlier_count,
            "inlier_ratio": round(r.inlier_ratio, 6),
            "tilt_deg": None if r.tilt_deg is None else round(r.tilt_deg, 4),
            "rms": None if r.rms is None else round(r.rms, 6),
            "iterations_used": r.iterations_used,
        }
        for r in out.chunk_reports
    ]
    return {
        "point_count": len(ids),
        "reliable_chunks": out.reliable_chunk_count,
        "undecidable_chunks": out.undecidable_chunk_count,
        "results": results,
        "chunk_reports": chunk_reports,
        "request_sha256": sha,
    }


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Echo locations/messages/types only: Pydantic's default body repeats the
    # offending input, which may be NaN/Infinity and cannot be JSON-encoded.
    safe_errors = [
        {
            "loc": ["body", *(str(part) for part in err.get("loc", ()) if part != "body")],
            "msg": err.get("msg", "invalid request"),
            "type": err.get("type", "value_error"),
        }
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe_errors})


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "ground-segmentation", "version": app.version}


@app.post("/segment")
def segment(req: SegmentRequest) -> JSONResponse:
    # Duplicate IDs would break the id<->row contract.
    ids = [p.id for p in req.points]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="point ids must be unique")

    ransac_cfg, chunk_cfg = _to_configs(req)
    arr = np.asarray(req.point_array(), dtype=np.float64)
    try:
        out = segment_cloud(arr, ransac_cfg, chunk_cfg)
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise HTTPException(status_code=500, detail=f"segmentation failed: {exc}")
    return JSONResponse(_serialize(out, ids, _request_hash(req)))
