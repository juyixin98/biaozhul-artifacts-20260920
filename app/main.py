"""FastAPI service entrypoint.

Error semantics
---------------
* 422 ``REJECTED_INVALID_INPUT``      – malformed payload/static shape contract.
* 409 ``REJECTED_VERSION_MISMATCH``   – requested version is not the deployed one.
* 409 ``REJECTED_NOT_CALIBRATED``     – model invoked before freeze (impossible
                                        through normal bootstrap, defensive).
* 422 ``UNDETERMINED_NUMERIC_CONTRACT`` – integer path ran but the result could
                                        not be verified against the references.
* 500 ``REJECTED_INTERNAL``           – unexpected server defect.

Every error and every response carries the same ``request_id``; logs include it
plus shapes/counts only, never payload values.
"""

from __future__ import annotations

import logging
import uuid

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .bootstrap import build_registry
from .config import Settings
from .errors import (
    CoreIntegrityError,
    InvalidInputError,
    NumericIndeterminacyError,
    QInferError,
)
from .kernel import AccumulatorOverflow
from .schemas import InferRequest, InferResponse, LayerResult, ModelInfo
from .verification import verify_trace

logger = logging.getLogger("qinfer")


class _RequestIdFilter(logging.Filter):
    """Ensure every record can be formatted even outside a request context."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


_handler = logging.StreamHandler()
_handler.addFilter(_RequestIdFilter())
_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s request_id=%(request_id)s %(message)s"
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])


def _log_extra(request_id: str) -> dict[str, str]:
    return {"request_id": request_id}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="tiny quantized matmul backend", version="1.0.0")
    app.state.settings = settings
    app.state.registry = build_registry(settings)

    @app.exception_handler(QInferError)
    async def _qerror_handler(_: Request, exc: QInferError) -> JSONResponse:
        if exc.request_id:
            logger.warning("%s details=%s", exc.message, exc.details,
                           extra=_log_extra(exc.request_id))
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(AccumulatorOverflow)
    async def _overflow_handler(_: Request, exc: AccumulatorOverflow) -> JSONResponse:
        # Integer envelope crossing: we cannot judge the numeric result.
        rid = "overflow"
        err = NumericIndeterminacyError(
            f"accumulator envelope crossed: {exc}",
            request_id=rid,
            details={"failure_class": "ACCUMULATOR_OVERFLOW"},
        )
        return JSONResponse(status_code=err.http_status, content=err.to_dict())

    @app.exception_handler(Exception)
    async def _unexpected_handler(_: Request, exc: Exception) -> JSONResponse:
        rid = "unexpected-" + uuid.uuid4().hex[:12]
        logger.exception("unexpected failure: %s", exc, extra=_log_extra(rid))
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "REJECTED_INTERNAL",
                    "verdict": "REJECTED",
                    "message": "internal error",
                    "request_id": rid,
                    "details": {},
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/models/{model_id}", response_model=ModelInfo)
    async def model_info(model_id: str) -> ModelInfo:
        entry = app.state.registry.get(model_id)
        return ModelInfo(
            model_id=entry.model.model_id,
            model_version=entry.state.model_version,
            calibration_fingerprint=entry.state.calibration_fingerprint,
            layer_order=entry.model.layer_order(),
            state="FROZEN",
        )

    @app.post("/infer", response_model=InferResponse)
    async def infer(req: InferRequest) -> InferResponse:
        request_id = req.request_id or f"req-{uuid.uuid4().hex[:12]}"
        try:
            m, k = req.shape
            if m * k != len(req.values):
                raise InvalidInputError(
                    "shape/values length mismatch: "
                    f"{m}*{k}={m * k} vs {len(req.values)}",
                    request_id=request_id,
                    details={"declared_shape": [m, k], "n_values": len(req.values)},
                )
            if m > settings.max_batch or k > settings.max_features:
                raise InvalidInputError(
                    "input exceeds configured limits",
                    request_id=request_id,
                    details={
                        "shape": [m, k],
                        "limits": {"max_batch": settings.max_batch,
                                   "max_features": settings.max_features},
                    },
                )
            x = np.asarray(req.values, dtype=np.float32).reshape(m, k)
            if not np.all(np.isfinite(x)):
                raise InvalidInputError(
                    "input contains NaN/Inf", request_id=request_id,
                    details={"shape": [m, k]},
                )

            try:
                entry = app.state.registry.get(req.model_id, req.model_version)
            except QInferError as exc:
                # Registry errors predate response assembly; bind the request id
                # so the rejection diagnosis stays reproducible end to end.
                exc.request_id = request_id
                raise
            expected_k = entry.model.layers[entry.model.layer_order()[0]].weight.codes.shape[1]
            if k != expected_k:
                raise InvalidInputError(
                    f"feature dimension {k} != model input dimension {expected_k}",
                    request_id=request_id,
                    details={"shape": [m, k], "expected_features": expected_k},
                )

            trace = entry.model.execute_float(x)
            report = verify_trace(
                entry.model, x, trace, request_id=request_id
            )

            logger.info(
                "verdict=%s shape=(%d,%d) layers=%d",
                report.verdict, m, k, len(report.layers),
                extra=_log_extra(request_id),
            )

            if report.verdict == "REJECTED":
                # Core defect: the integer result disagrees with the
                # independent oracle. Refuse to return it; not the caller's
                # fault, so this is a 5xx integrity failure.
                raise CoreIntegrityError(
                    "integer result rejected by independent verification",
                    request_id=request_id,
                    details={"verification": report.as_dict()},
                )

            layer_payloads = [
                LayerResult(
                    name=name,
                    output_shape=list(io.output_float.shape),
                    output_codes=io.integer_result.output.codes.tolist(),
                    saturated_outputs=report.layers[idx].saturated_outputs,
                    dequantized=np.round(io.output_float.astype(np.float64), 6).tolist(),
                )
                for idx, (name, io) in enumerate(trace.items())
            ]
            return InferResponse(
                request_id=request_id,
                model_id=entry.model.model_id,
                model_version=entry.state.model_version,
                calibration_fingerprint=entry.state.calibration_fingerprint,
                verdict=report.verdict,
                verification=report.as_dict(),
                layers=layer_payloads,
            )
        except QInferError:
            raise
        except AccumulatorOverflow as exc:
            raise NumericIndeterminacyError(
                f"accumulator envelope crossed: {exc}",
                request_id=request_id,
                details={"failure_class": "ACCUMULATOR_OVERFLOW"},
            ) from exc

    return app


app = create_app()
