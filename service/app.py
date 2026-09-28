"""FastAPI service entry: mixed-precision training runs over HTTP.

Error semantics (see README):
- 404  unknown run_id (or no checkpoint for the run)
- 409  run_id already exists
- 422  invalid trainer configuration (ConfigError) or invalid step
       parameters (n < 1, amplify <= 0, batch_size < 1)
- Gradient overflow is NOT an HTTP error: it is a normal training outcome
  reported inside the step record as decision="skipped_overflow".

Run:  uvicorn service.app:app --app-dir src --port 8000
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mptrainer.checkpoint import load_checkpoint, save_checkpoint
from mptrainer.config import ConfigError, TrainerConfig
from mptrainer.data import SyntheticBatchSource
from mptrainer.runlog import runtime_versions
from mptrainer.trainer import MixedPrecisionTrainer

CHECKPOINT_ROOT = Path(os.environ.get("MPTRAINER_CKPT_DIR", "checkpoints"))
LOG_ROOT = Path(os.environ.get("MPTRAINER_LOG_DIR", "logs"))

app = FastAPI(title="mptrainer", version="0.1.0")


class RunHandle:
    """A trainer plus its private synthetic data source."""

    def __init__(self, trainer: MixedPrecisionTrainer, source: SyntheticBatchSource) -> None:
        self.trainer = trainer
        self.source = source


RUNS: dict[str, RunHandle] = {}


class CreateRunRequest(BaseModel):
    run_id: str | None = None
    config: dict[str, Any] | None = None


class StepRequest(BaseModel):
    n: int = Field(default=1, ge=1)
    batch_size: int = Field(default=16, ge=1)
    amplify: float = Field(default=1.0, gt=0.0)


def _get_run(run_id: str) -> RunHandle:
    try:
        return RUNS[run_id]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}") from None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/version")
def version() -> dict:
    return runtime_versions()


@app.post("/runs", status_code=201)
def create_run(req: CreateRunRequest) -> dict:
    try:
        config = TrainerConfig.from_dict(req.config) if req.config else TrainerConfig().validate()
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run_id = req.run_id or f"run-{uuid.uuid4().hex[:12]}"
    if run_id in RUNS:
        raise HTTPException(status_code=409, detail=f"run_id already exists: {run_id}")
    trainer = MixedPrecisionTrainer(config, run_id=run_id, log_dir=LOG_ROOT)
    source = SyntheticBatchSource(config.layer_sizes[0], config.layer_sizes[-1])
    RUNS[run_id] = RunHandle(trainer, source)
    return {"run_id": run_id, "state": trainer.state_summary()}


@app.post("/runs/{run_id}/steps")
def run_steps(run_id: str, req: StepRequest) -> dict:
    handle = _get_run(run_id)
    records = []
    for _ in range(req.n):
        x, y = handle.source.batch(req.batch_size, amplify=req.amplify)
        records.append(handle.trainer.train_step(x, y).to_dict())
    return {"run_id": run_id, "records": records, "state": handle.trainer.state_summary()}


@app.get("/runs/{run_id}/state")
def run_state(run_id: str) -> dict:
    return _get_run(run_id).trainer.state_summary()


@app.get("/runs/{run_id}/log")
def run_log(run_id: str) -> dict:
    return {"run_id": run_id, "records": _get_run(run_id).trainer.logger.records}


@app.post("/runs/{run_id}/checkpoint")
def checkpoint(run_id: str) -> dict:
    handle = _get_run(run_id)
    path = save_checkpoint(handle.trainer, CHECKPOINT_ROOT / run_id)
    return {"run_id": run_id, "checkpoint": str(path)}


@app.post("/runs/{run_id}/restore")
def restore(run_id: str) -> dict:
    handle = _get_run(run_id)
    directory = CHECKPOINT_ROOT / run_id
    if not directory.exists():
        raise HTTPException(status_code=404, detail=f"no checkpoint for run_id: {run_id}")
    restored = load_checkpoint(directory, run_id=run_id, log_dir=LOG_ROOT)
    RUNS[run_id] = RunHandle(restored, handle.source)
    return {"run_id": run_id, "state": restored.state_summary()}
