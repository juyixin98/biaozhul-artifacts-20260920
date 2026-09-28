"""FastAPI application: typed error envelope, inference and validation."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from engine.errors import DecisionCategory, QuantEngineError
from service.config import Settings
from service.inference import InferenceService
from service.logging_conf import configure_logging, log_failure
from service.registry import ModelRegistry


def create_app(
    registry: ModelRegistry | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    logger = configure_logging(settings.log_level)
    registry = registry or ModelRegistry()
    service = InferenceService(
        registry,
        max_batch_size=settings.max_batch_size,
        max_features=settings.max_features,
        enforce_calibration_range=settings.enforce_calibration_range,
    )

    app = FastAPI(
        title="Small Integer MAC Inference Backend",
        version="1.0.0",
    )
    app.state.registry = registry
    app.state.settings = settings

    @app.exception_handler(QuantEngineError)
    async def handle_engine_error(
        request: Request, exc: QuantEngineError
    ) -> JSONResponse:
        # Echo the client request id when it supplied one; errors from the
        # service layer already carry the generated id in context.
        request_id = str(exc.context.get("request_id", "-"))
        log_failure(
            logger,
            request_id=request_id,
            code=exc.code,
            category=exc.category.value,
            message=exc.message,
            context=exc.context,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {
                    "code": exc.code,
                    "category": exc.category.value,
                    "message": exc.message,
                    "request_id": request_id,
                    # Context is already shape/scalar-only; redact again at the
                    # boundary as defense in depth.
                    "details": _safe_details(exc.context),
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/models")
    async def models() -> dict[str, Any]:
        return {"models": registry.ids()}

    @app.get("/models/{model_id}")
    async def model_metadata(model_id: str) -> dict[str, Any]:
        artifact = registry.get(model_id)
        return {
            "model_id": artifact.model_id,
            "model_version": artifact.model_version,
            "quantizer_version": artifact.quantizer_version,
            "architecture": list(artifact.architecture),
            "calibration": artifact.calibration.to_dict(),
        }

    @app.post("/models/{model_id}/infer")
    async def infer(model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = InferenceService.new_request_id()
        response = service.infer(
            model_id, payload.get("input"), request_id=request_id
        )
        return {
            "request_id": response.request_id,
            "model_id": response.model_id,
            "model_version": response.model_version,
            "output": response.output,
            "diagnostics": response.diagnostics,
        }

    @app.post("/models/{model_id}/validate")
    async def validate(model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = InferenceService.new_request_id()
        response = service.validate(
            model_id, payload.get("input"), request_id=request_id
        )
        return {
            "request_id": response.request_id,
            "model_id": response.model_id,
            "decision": (
                DecisionCategory.ACCEPT.value
                if response.accepted
                else DecisionCategory.INDETERMINATE.value
            ),
            "reason": response.reason,
            "error_report": response.error_report,
            "diagnostics": response.diagnostics,
        }

    return app


def _safe_details(context: dict[str, Any]) -> dict[str, Any]:
    from service.logging_conf import redact_context

    return {
        k: v
        for k, v in redact_context(context).items()
        if k != "request_id"
    }


app = create_app()
