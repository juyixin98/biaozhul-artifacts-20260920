"""FastAPI service entrypoint.

Run locally with::

    uvicorn amptrain.api:app --reload

Error semantics: every failure response uses the envelope
``{"success": false, "error": {"code", "message", "detail"}}`` with a 4xx/5xx
status.  Unknown exceptions are reported as ``INTERNAL_ERROR`` -- never
remapped to a success response.
"""

from __future__ import annotations

import traceback
from dataclasses import asdict
from typing import Any

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import (
    AccumulationConfig,
    DataConfig,
    ModelConfig,
    OptimizerConfig,
    PrecisionConfig,
    RunConfig,
    ScalerConfig,
)
from .errors import AmpTrainError, ErrorCode
from .events import VERSION_CONTEXT
from .schemas import (
    AutoWindowsIn,
    CreateRunIn,
    CustomWindowIn,
    CheckpointIn,
    LoadCheckpointIn,
)
from .service import RunRegistry
from .checkpoint import load_checkpoint, save_checkpoint
from .trainer import WindowOutcome

app = FastAPI(
    title="amptrain",
    version=VERSION_CONTEXT["amptrain"],
    description="Mixed-precision trainer for small synthetic networks.",
)
registry = RunRegistry()


def _error_envelope(code: str, message: str, detail: dict[str, Any] | None = None) -> dict:
    return {"success": False, "error": {"code": code, "message": message, "detail": detail or {}}}


def _success(payload: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, **payload}


@app.exception_handler(AmpTrainError)
async def _handle_amp_error(_: Request, exc: AmpTrainError) -> JSONResponse:
    status = 404 if exc.code in (ErrorCode.RUN_NOT_FOUND, ErrorCode.CHECKPOINT_NOT_FOUND) else 400
    return JSONResponse(status_code=status, content=_error_envelope(exc.code.value, exc.message, exc.detail))


@app.exception_handler(Exception)
async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content=_error_envelope(
            ErrorCode.INTERNAL_ERROR.value,
            f"unexpected error: {type(exc).__name__}: {exc}",
        ),
    )


def _build_config(body: CreateRunIn) -> RunConfig:
    return RunConfig(
        model=ModelConfig(**body.model_dump()["model"]),
        precision=PrecisionConfig(**body.model_dump()["precision"]),
        scaler=ScalerConfig(**body.model_dump()["scaler"]),
        accumulation=AccumulationConfig(**body.model_dump()["accumulation"]),
        optimizer=OptimizerConfig(**body.model_dump()["optimizer"]),
        data=DataConfig(**body.model_dump()["data"]),
        seed=body.seed,
        batch_size=body.batch_size,
    )


def _outcome_dict(outcome: WindowOutcome) -> dict[str, Any]:
    data = asdict(outcome)
    data["nonfinite_tensors"] = list(data["nonfinite_tensors"])
    return data


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return _success({"status": "ok", "versions": VERSION_CONTEXT})


@app.get("/version")
async def version() -> dict[str, Any]:
    return _success({"versions": VERSION_CONTEXT})


@app.post("/runs", status_code=201)
async def create_run(body: CreateRunIn) -> dict[str, Any]:
    config = _build_config(body)
    trainer = registry.create(
        config, n_samples=body.n_samples, amplification=body.amplification
    )
    return _success({"run": trainer.stats()})


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    return _success({"run": registry.get(run_id).stats()})


@app.post("/runs/{run_id}/windows/auto")
async def run_auto_windows(run_id: str, body: AutoWindowsIn) -> dict[str, Any]:
    trainer = registry.get(run_id)
    outcomes = [
        _outcome_dict(trainer.process_window(trainer.next_fixture_window()))
        for _ in range(body.n_windows)
    ]
    return _success({"outcomes": outcomes, "run": trainer.stats()})


@app.post("/runs/{run_id}/windows/custom")
async def run_custom_window(run_id: str, body: CustomWindowIn) -> dict[str, Any]:
    trainer = registry.get(run_id)
    batches = [
        (np.asarray(b.x, dtype=np.float32), np.asarray(b.y, dtype=np.float32))
        for b in body.batches
    ]
    outcome = trainer.process_window(batches)
    return _success({"outcome": _outcome_dict(outcome), "run": trainer.stats()})


@app.get("/runs/{run_id}/events")
async def get_events(run_id: str, limit: int = 100) -> dict[str, Any]:
    trainer = registry.get(run_id)
    events = trainer.log.to_list()
    return _success({"events": events[-limit:], "count": len(events)})


@app.post("/runs/{run_id}/checkpoint")
async def save_run_checkpoint(run_id: str, body: CheckpointIn) -> dict[str, Any]:
    trainer = registry.get(run_id)
    path = save_checkpoint(trainer, body.directory)
    return _success({"checkpoint": {"path": str(path), "scale": trainer.scaler.scale}})


@app.post("/checkpoints/load", status_code=201)
async def load_run(body: LoadCheckpointIn) -> dict[str, Any]:
    fixture = registry.rebuild_fixture(_peek_run_id(body.directory)) if body.attach_fixture else None
    trainer = load_checkpoint(body.directory, fixture=fixture)
    registry.register_loaded(trainer)
    return _success({"run": trainer.stats()})


def _peek_run_id(directory: str) -> str:
    """Best-effort run id lookup for fixture re-attachment; missing is fine."""
    import json
    from pathlib import Path

    meta = Path(directory) / "meta.json"
    if meta.exists():
        try:
            return str(json.loads(meta.read_text(encoding="utf-8")).get("run_id", ""))
        except OSError:
            return ""
    return ""
