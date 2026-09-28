"""FastAPI surface: train/restore, commit inspection, strict validation, parity.

All responses echo a ``request_id``; failures use typed categories and keep
``failures`` (hard) separate from ``uncertainties`` (conclusions within a
narrow band of tolerance).
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .errors import CheckpointError
from .fixtures import AppConfig
from .logging_setup import configure_logging
from .service import describe_commits, train, validate_commit, verify_restore_parity

logger = logging.getLogger("adam_shard.api")
_CONFIG: AppConfig | None = None


def get_config() -> AppConfig:
    if _CONFIG is None:
        raise RuntimeError("config not initialized; call create_app(config)")
    return _CONFIG


class TrainRequest(BaseModel):
    world_size: int = Field(gt=0, le=64)
    steps: int | None = Field(default=None, ge=0)
    model_commit: str | None = None
    optim_commit: str | None = None
    request_id: str | None = None


class ValidateRequest(BaseModel):
    commit_id: str
    target_world_size: int = Field(gt=0, le=64)


class ParityRequest(BaseModel):
    source_commit: str
    target_world_size: int = Field(gt=0, le=64)
    request_id: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    cfg = get_config()
    os.makedirs(cfg.storage_root, exist_ok=True)
    logger.info("storage_root=%s version=%s", cfg.storage_root, __version__)
    yield


def create_app(config: AppConfig | None = None) -> FastAPI:
    global _CONFIG
    if config is not None:
        _CONFIG = config
    elif _CONFIG is None:
        _CONFIG = AppConfig.load()
    app = FastAPI(title="adam-shard", version=__version__, lifespan=lifespan)

    def run_with_error_handling(fn):
        def execute(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except CheckpointError as exc:
                logger.warning("request rejected category=%s: %s", exc.category, exc)
                raise HTTPException(
                    status_code=422,
                    detail={
                        "ok": False,
                        "category": exc.category,
                        "message": str(exc),
                        "commit_id": exc.commit_id,
                        "extra": exc.detail,
                    },
                ) from exc

        return execute

    @app.get("/health")
    def health():
        cfg = get_config()
        return {"status": "ok", "version": __version__, "storage_root": cfg.storage_root}

    @app.get("/commits")
    def commits():
        return {"request_id": f"req-{uuid.uuid4().hex[:12]}", "commits": run_with_error_handling(describe_commits)(get_config())}

    @app.post("/train")
    def do_train(req: TrainRequest):
        request_id = req.request_id or f"req-{uuid.uuid4().hex[:12]}"
        logger.info(
            "[req=%s] POST /train world_size=%s steps=%s restore=%s",
            request_id, req.world_size, req.steps,
            req.model_commit or req.optim_commit or "<fresh>",
        )
        outcome = run_with_error_handling(train)(
            get_config(),
            req.world_size,
            steps=req.steps,
            request_id=request_id,
            model_commit=req.model_commit,
            optim_commit=req.optim_commit,
        )
        return {
            "ok": True,
            "request_id": request_id,
            "commit_id": outcome.commit_id,
            "world_size": outcome.world_size,
            "step": outcome.step,
            "restored_from": outcome.restored_from,
            "final_rank_losses": list(outcome.losses),
        }

    @app.post("/validate")
    def do_validate(req: ValidateRequest):
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        result = run_with_error_handling(validate_commit)(get_config(), req.commit_id, req.target_world_size)
        result["request_id"] = request_id
        return result

    @app.post("/verify-parity")
    def do_parity(req: ParityRequest):
        request_id = req.request_id or f"req-{uuid.uuid4().hex[:12]}"
        logger.info(
            "[req=%s] POST /verify-parity commit=%s -> world_size=%s",
            request_id, req.source_commit, req.target_world_size,
        )
        report = run_with_error_handling(verify_restore_parity)(
            get_config(), req.source_commit, req.target_world_size, request_id
        )
        return report.to_dict()

    return app


app = create_app()
