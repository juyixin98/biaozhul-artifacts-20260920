"""FastAPI application exposing the finality gadget as a JSON API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import settings
from .db import Store
from .service import FinalityService, ServiceError, ValidatorSpec


# ------------------------------------------------------------------- models


class ValidatorIn(BaseModel):
    validator_id: str = Field(min_length=1)
    weight: int = Field(ge=0)
    public_key: str = Field(min_length=1)


class CreateEpochIn(BaseModel):
    epoch_id: int = Field(ge=0)
    height: int = Field(ge=0)
    validators: list[ValidatorIn] = Field(min_length=1)


class VoteIn(BaseModel):
    epoch_id: int = Field(ge=0)
    height: int = Field(ge=0)
    validator_id: str = Field(min_length=1)
    value: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class CertSignatureIn(BaseModel):
    validator_id: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class CertificateIn(BaseModel):
    epoch_id: int = Field(ge=0)
    height: int = Field(ge=0)
    value: str = Field(min_length=1)
    signatures: list[CertSignatureIn] = Field(min_length=1)


# ---------------------------------------------------------------------- app


def create_app(db_path: str | None = None, chain_id: str | None = None) -> FastAPI:
    store = Store(db_path or settings.db_path, reset=settings.reset_on_start)
    service = FinalityService(store, chain_id or settings.chain_id)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # On boot, rebuild every conclusion from raw persisted votes so a
        # restart reproduces exactly the same finality view.
        app.state.rebuild_report = service.rebuild_conclusions()
        yield
        store.close()

    app = FastAPI(title="Finality Divergence Detector", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.service = service

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "chain_id": service.chain_id,
            "rebuild": app.state.rebuild_report,
        }

    @app.post("/epochs", status_code=201)
    def create_epoch(body: CreateEpochIn) -> dict[str, Any]:
        specs = [
            ValidatorSpec(v.validator_id, v.weight, v.public_key) for v in body.validators
        ]
        return service.create_epoch(body.epoch_id, body.height, specs)

    @app.get("/epochs/{epoch_id}")
    def epoch_status(epoch_id: int) -> dict[str, Any]:
        return service.epoch_status(epoch_id)

    @app.post("/votes")
    def submit_vote(body: VoteIn) -> dict[str, Any]:
        return service.submit_vote(
            body.epoch_id, body.height, body.validator_id, body.value, body.signature
        )

    @app.post("/certificates")
    def submit_certificate(body: CertificateIn) -> dict[str, Any]:
        return service.submit_conflicting_certificate(
            body.epoch_id,
            body.height,
            body.value,
            [s.model_dump() for s in body.signatures],
        )

    @app.get("/epochs/{epoch_id}/evidence")
    def evidence(epoch_id: int) -> dict[str, Any]:
        return {"epoch_id": epoch_id, "exclusions": service.evidence_for(epoch_id)}

    @app.get("/alarms")
    def alarms(epoch_id: int | None = None) -> dict[str, Any]:
        return {"alarms": service.list_alarms(epoch_id)}

    @app.get("/rebuild")
    def rebuild() -> dict[str, Any]:
        return service.rebuild_conclusions()

    return app


app = create_app()
