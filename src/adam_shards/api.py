"""FastAPI service exposing shard save/restore/reshard and verification.

Every response or error is correlated with a request id (client-supplied
``X-Request-ID`` or a generated one). Checkpoint failures are serialized to
their specific category, and uncertain conclusions are reported separately
from hard failures.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import AppConfig
from .errors import CheckpointError
from .pipeline import run_pipeline
from .sharding import read_manifest, reshard_checkpoint
from .logging_utils import get_logger


class PipelineRequest(BaseModel):
    save_world_size: int = Field(gt=0)
    restore_world_size: int = Field(gt=0)
    train_steps: int = Field(default=4, gt=0)
    tol: float = Field(default=1e-12, gt=0)


class ReshardRequest(BaseModel):
    src_dir: str
    dst_dir: str
    new_world_size: int = Field(gt=0)


class ManifestRequest(BaseModel):
    ckpt_dir: str


def create_app(config: AppConfig | None = None) -> FastAPI:
    app = FastAPI(
        title="adam-shards",
        version="1.0.0",
        description="Local multiprocess Adam state sharding with resharding",
    )
    app.state.config = config

    @app.exception_handler(CheckpointError)
    async def _on_checkpoint_error(request: Request, exc: CheckpointError) -> JSONResponse:
        rid = exc.request_id or request.headers.get("x-request-id")
        exc.request_id = rid
        logger = get_logger(rid, stage="error")
        logger.error("%s: %s", exc.category, exc)
        return JSONResponse(status_code=422, content=exc.to_dict())

    @app.get("/health")
    async def health(x_request_id: str | None = Header(default=None)) -> dict:
        rid = x_request_id or _new_request_id()
        return {"ok": True, "service": "adam-shards", "version": "1.0.0",
                "request_id": rid}

    @app.post("/verify/pipeline")
    async def verify_pipeline(
        body: PipelineRequest,
        x_request_id: str | None = Header(default=None),
    ) -> dict:
        cfg = app.state.config
        if cfg is None:
            return JSONResponse(status_code=503, content={
                "ok": False, "category": "not_configured",
                "error": "service started without a config file",
                "request_id": x_request_id,
            })
        rid = x_request_id or _new_request_id()
        logger = get_logger(rid, stage="pipeline")
        import tempfile
        with tempfile.TemporaryDirectory(prefix="adam-ckpt-") as ckpt_dir:
            result = run_pipeline(
                dims=cfg.layer_dims, seed=cfg.seed,
                n_samples=cfg.batch_size * body.train_steps + cfg.batch_size,
                batch_size=cfg.batch_size,
                train_steps=body.train_steps,
                adam_cfg=cfg.adam,
                save_world_size=body.save_world_size,
                restore_world_size=body.restore_world_size,
                ckpt_dir=ckpt_dir, request_id=rid, tol=body.tol,
                logger=logger,
            )
        payload = result.to_dict()
        payload["ok"] = payload["passed"]
        return payload

    @app.post("/checkpoints/reshard")
    async def reshard(
        body: ReshardRequest,
        x_request_id: str | None = Header(default=None),
    ) -> dict:
        rid = x_request_id or _new_request_id()
        logger = get_logger(rid, stage="reshard")
        new_commit = reshard_checkpoint(
            body.src_dir, body.dst_dir, body.new_world_size,
            request_id=rid, logger=logger,
        )
        return {"ok": True, "request_id": rid, "new_commit_id": new_commit,
                "new_world_size": body.new_world_size,
                "dst_dir": body.dst_dir}

    @app.post("/checkpoints/manifest")
    async def manifest(
        body: ManifestRequest,
        x_request_id: str | None = Header(default=None),
    ) -> dict:
        rid = x_request_id or _new_request_id()
        logger = get_logger(rid, stage="manifest")
        m = read_manifest(body.ckpt_dir)
        logger.info("served manifest commit=%s world_size=%s",
                    m.get("commit_id"), m.get("world_size"))
        return {"ok": True, "request_id": rid, "manifest": m}

    return app


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def create_configured_app(config_path: str | Path) -> FastAPI:
    return create_app(AppConfig.load(config_path))
