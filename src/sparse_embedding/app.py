"""FastAPI application exposing the sparse embedding optimizer service.

Error mapping is explicit:

* domain ``batch_rejected``      -> HTTP 400 (whole batch rejected)
* domain ``empty_batch``         -> HTTP 200 with ``verdict="empty"`` (a defined no-op)
* domain ``persistence_error``   -> HTTP 500
* domain ``state_shape_error``   -> HTTP 409 (checkpoint/config conflict)

Nothing collapses an unknown exception into 200.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .config import ServiceConfig, env_config
from .errors import (
    EmptyBatchError,
    PersistenceError,
    StateShapeError,
    ValidationBatchRejectedError,
)
from .journal import RunJournal, new_run_id
from .schemas import (
    BatchResultOut,
    GradientBatchIn,
    PersistOut,
    RowOut,
    SummaryOut,
)
from .service import SparseEmbeddingService


def create_app(config: ServiceConfig | None = None) -> FastAPI:
    config = config or env_config()
    log_path = os.environ.get("SPARSE_EMB_LOG", os.path.join(config.state_dir, "journal.jsonl"))
    journal = RunJournal(log_path)
    service = SparseEmbeddingService(config, journal)
    service.load_if_present()

    app = FastAPI(
        title="Sparse Embedding Gradient Update Service",
        version=__import__("sparse_embedding", fromlist=["__version__"]).__version__,
    )
    app.state.service = service

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", **service.summary()}

    @app.get("/state/summary", response_model=SummaryOut)
    def state_summary() -> dict[str, Any]:
        return service.summary()

    @app.get("/state/row/{index}", response_model=RowOut)
    def state_row(index: int) -> dict[str, Any]:
        try:
            return service.row(index)
        except IndexError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/apply", response_model=BatchResultOut)
    def apply_batch(payload: GradientBatchIn) -> JSONResponse:
        run_id = payload.run_id or new_run_id()
        try:
            report = service.apply(
                payload.indices,
                payload.values,
                run_id=run_id,
                batch_id=payload.batch_id,
            )
        except EmptyBatchError as exc:
            # Defined no-op category, reported honestly rather than as an
            # applied update. Step counter is unchanged.
            body = {
                "run_id": run_id,
                "batch_id": payload.batch_id,
                "verdict": "empty",
                "raw_row_count": 0,
                "n_unique": 0,
                "n_active": 0,
                "n_zero_skipped": 0,
                "active_indices": [],
                "zero_skipped_indices": [],
                "unique_indices": [],
                "global_step_before": service.state.global_step,
                "global_step_after": service.state.global_step,
                "max_abs_delta": 0.0,
                "clip": {"mode": service.config.clip.mode.value},
                "error_code": exc.code,
            }
            return JSONResponse(status_code=200, content=body)
        except ValidationBatchRejectedError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error_code": exc.code,
                    "message": exc.message,
                    "run_id": run_id,
                    "details": exc.details,
                },
            )
        except (PersistenceError, StateShapeError) as exc:
            return JSONResponse(
                status_code=500 if isinstance(exc, PersistenceError) else 409,
                content={
                    "error_code": exc.code,
                    "message": exc.message,
                    "run_id": run_id,
                    "details": exc.details,
                },
            )

        body = {
            "run_id": report.run_id,
            "batch_id": report.batch_id,
            "verdict": "applied" if report.n_active else "applied_zero_only",
            "raw_row_count": report.raw_row_count,
            "n_unique": int(report.unique_indices.shape[0]),
            "n_active": report.n_active,
            "n_zero_skipped": report.n_zero_skipped,
            "active_indices": report.active_indices.tolist(),
            "zero_skipped_indices": report.zero_skipped_indices.tolist(),
            "unique_indices": report.unique_indices.tolist(),
            "global_step_before": report.global_step_before,
            "global_step_after": report.global_step_after,
            "max_abs_delta": report.max_abs_delta,
            "clip": report.clip_report,
        }
        return JSONResponse(status_code=200, content=body)

    @app.post("/checkpoint", response_model=PersistOut)
    def checkpoint() -> dict[str, Any]:
        try:
            return service.checkpoint()
        except PersistenceError as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "error_code": exc.code,
                    "message": exc.message,
                    "run_id": "n/a",
                    "details": exc.details,
                },
            )

    return app


app = create_app()
