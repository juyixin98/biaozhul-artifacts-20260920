"""FastAPI application.

Permission model (allocation concealment vs audit isolation)
------------------------------------------------------------
* ``enroller``: register subjects and receive *only* the assigned arm for
  the requested subject. Block sizes, positions, permutations, seeds and
  future allocations are never exposed.
* ``auditor``: read-only access to audit events, diagnostics and effect
  estimates; the master seed is redacted (server-side replay proves
  agreement without handing over predictability).
* ``administrator``: register/seal studies, enter outcomes, and see the
  master seed.

Every response uses one envelope::

    {"success": bool, "data": ... | None,
     "error": {"category", "message", "details"} | None,
     "meta": {"request_id", "service_version", "endpoint"}}
"""
from __future__ import annotations

import logging
import sys
import time
import uuid
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Config
from .contract import (
    AllocationError,
    ErrorCategory,
    TailPolicy,
    build_study_config,
    validate_features,
)
from .diagnostics import diagnose_study
from .estimator import build_effect_report
from .replay import replay_from_audit
from .rng import draw_next
from .storage import Storage

ROLE_ENROLLER = "enroller"
ROLE_AUDITOR = "auditor"
ROLE_ADMIN = "administrator"

logger = logging.getLogger("stratblock")


def configure_logging() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# --------------------------------------------------------------------- schemas


class StudyRegisterRequest(BaseModel):
    study_id: str = Field(min_length=1, max_length=128)
    arms: list[str]
    stratification_factors: list[str]
    block_sizes: list[int]
    allocation_ratio: list[int] | None = None
    tail_policy: str = TailPolicy.PERMUTED.value


class EnrollRequest(BaseModel):
    subject_id: str = Field(min_length=1, max_length=128)
    features: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=160)


class OutcomeRequest(BaseModel):
    subject_id: str = Field(min_length=1, max_length=128)
    y: float


# ---------------------------------------------------------------- application


class Service:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.from_env()
        self.storage = Storage(self.config.db_path)

    def close(self) -> None:
        self.storage.close()


def create_app(config: Config | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Stratified Block Allocation Service",
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    service = Service(config)
    app.state.service = service

    def envelope(
        data: Any, endpoint: str, request_id: str | None
    ) -> dict[str, Any]:
        return {
            "success": True,
            "data": data,
            "error": None,
            "meta": {
                "request_id": request_id,
                "service_version": __version__,
                "endpoint": endpoint,
            },
        }

    def require_role(*allowed: str) -> Callable[..., str]:
        def dependency(authorization: str | None = Header(default=None)) -> str:
            if not authorization or not authorization.startswith("Bearer "):
                raise AllocationError(
                    ErrorCategory.UNAUTHENTICATED,
                    "missing Bearer token",
                    {"required_roles": list(allowed)},
                    http_status=401,
                )
            token = authorization.removeprefix("Bearer ").strip()
            role = service.config.token_roles().get(token)
            if role is None:
                raise AllocationError(
                    ErrorCategory.UNAUTHENTICATED,
                    "unknown API token",
                    {"required_roles": list(allowed)},
                    http_status=401,
                )
            if role not in allowed:
                raise AllocationError(
                    ErrorCategory.FORBIDDEN,
                    f"role {role!r} may not access this endpoint",
                    {"given_role": role, "required_roles": list(allowed)},
                    http_status=403,
                )
            return role

        return dependency

    @app.middleware("http")
    async def request_logging(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                f'{{"event":"request_failed","request_id":"{request_id}",'
                f'"path":"{request.url.path}","service_version":"{__version__}"}}'
            )
            raise
        elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            f'{{"event":"request","request_id":"{request_id}",'
            f'"method":"{request.method}","path":"{request.url.path}",'
            f'"status_code":{response.status_code},"elapsed_ms":{elapsed_ms},'
            f'"service_version":"{__version__}"}}'
        )
        return response

    @app.exception_handler(AllocationError)
    async def domain_error_handler(request: Request, exc: AllocationError):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id")
        body = {
            "success": False,
            "data": None,
            "error": {
                "category": exc.category.value,
                "message": exc.message,
                "details": exc.details,
            },
            "meta": {
                "request_id": request_id,
                "service_version": __version__,
                "endpoint": request.url.path,
            },
        }
        logger.info(
            f'{{"event":"domain_error","request_id":"{request_id}",'
            f'"path":"{request.url.path}","category":"{exc.category.value}"}}'
        )
        return JSONResponse(status_code=exc.http_status, content=body)

    # --------------------------------------------------------- infrastructure

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service_version": __version__}

    @app.get("/version", tags=["meta"])
    async def version() -> dict[str, str]:
        return {"service": "stratblock", "version": __version__,
                "stream_scheme": "stratblock-deterministic-rng-v1"}

    # ---------------------------------------------------------------- studies

    @app.post("/v1/studies", tags=["studies"])
    async def register_study(
        body: StudyRegisterRequest,
        role: str = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, Any]:
        cfg = build_study_config(
            study_id=body.study_id,
            arms=body.arms,
            stratification_factors=body.stratification_factors,
            block_sizes=body.block_sizes,
            allocation_ratio=body.allocation_ratio,
            tail_policy=body.tail_policy,
        )
        service.storage.register_study(cfg, service.config.master_seed)
        logger.info(
            f'{{"event":"study_registered","study_id":"{cfg.study_id}",'
            f'"actor":"{role}","arms":{len(cfg.arms)}}}'
        )
        return envelope(
            {"study_id": cfg.study_id, "status": "registered",
             "config": _public_config(cfg, include_seed=(role == ROLE_ADMIN),
                                      master_seed=service.config.master_seed)},
            "/v1/studies", None,
        )

    @app.get("/v1/studies", tags=["studies"])
    async def list_studies(
        role: str = Depends(require_role(ROLE_AUDITOR, ROLE_ADMIN)),
    ) -> dict[str, Any]:
        studies = []
        for item in service.storage.list_studies():
            studies.append(
                {
                    "study_id": item["study_id"],
                    "status": item["status"],
                    "created_at": item["created_at"],
                    "config": item["config"],
                    "master_seed": item["master_seed"] if role == ROLE_ADMIN else None,
                }
            )
        return envelope(studies, "/v1/studies", None)

    @app.get("/v1/studies/{study_id}", tags=["studies"])
    async def get_study(
        study_id: str,
        role: str = Depends(require_role(ROLE_AUDITOR, ROLE_ADMIN)),
    ) -> dict[str, Any]:
        cfg, seed = service.storage.get_study(study_id)
        return envelope(
            {
                "config": _public_config(
                    cfg, include_seed=(role == ROLE_ADMIN), master_seed=seed
                ),
            },
            f"/v1/studies/{study_id}", None,
        )

    @app.post("/v1/studies/{study_id}/seal", tags=["studies"])
    async def seal_study(
        study_id: str,
        role: str = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, Any]:
        service.storage.get_study(study_id)  # 404 if unknown
        service.storage.seal_study(study_id)
        return envelope(
            {"study_id": study_id, "status": "sealed"},
            f"/v1/studies/{study_id}/seal", None,
        )

    # ------------------------------------------------------------- enrollment

    @app.post("/v1/studies/{study_id}/enroll", tags=["enrollment"])
    async def enroll(
        study_id: str,
        body: EnrollRequest,
        role: str = Depends(require_role(ROLE_ENROLLER, ROLE_ADMIN)),
    ) -> dict[str, Any]:
        cfg, master_seed = service.storage.get_study(study_id)
        features = validate_features(cfg, body.features)
        result = service.storage.enroll(
            cfg, master_seed, body.subject_id, features,
            body.request_id, role, draw_next,
        )
        logger.info(
            f'{{"event":"allocation_decision","request_id":"{body.request_id}",'
            f'"study_id":"{study_id}","subject_id":"{body.subject_id}",'
            f'"arm":"{result.arm}","replayed":{str(result.replayed).lower()},'
            f'"actor":"{role}"}}'
        )
        # Concealment: enrollers receive no block size / position / seed.
        data = {
            "study_id": study_id,
            "subject_id": result.subject_id,
            "request_id": body.request_id,
            "arm": result.arm,
            "replayed": result.replayed,
            "allocated_at": result.allocated_at,
            "concealment": (
                "block sizes, positions, permutations and the master seed "
                "are withheld from the enrollment role"
            ),
        }
        return envelope(data, f"/v1/studies/{study_id}/enroll", body.request_id)

    # ------------------------------------------------------------------ audit

    @app.get("/v1/studies/{study_id}/audit", tags=["audit"])
    async def audit(
        study_id: str,
        role: str = Depends(require_role(ROLE_AUDITOR, ROLE_ADMIN)),
    ) -> dict[str, Any]:
        service.storage.get_study(study_id)
        return envelope(
            {"study_id": study_id,
             "events": service.storage.audit_events(study_id)},
            f"/v1/studies/{study_id}/audit", None,
        )

    @app.get("/v1/studies/{study_id}/diagnostics", tags=["audit"])
    async def diagnostics(
        study_id: str,
        role: str = Depends(require_role(ROLE_AUDITOR, ROLE_ADMIN)),
    ) -> dict[str, Any]:
        report = diagnose_study(service.storage, study_id)
        if role != ROLE_ADMIN:
            report = _redact_seed(report)
        return envelope(report, f"/v1/studies/{study_id}/diagnostics", None)

    @app.post("/v1/studies/{study_id}/replay", tags=["audit"])
    async def replay(
        study_id: str,
        role: str = Depends(require_role(ROLE_AUDITOR, ROLE_ADMIN)),
    ) -> dict[str, Any]:
        report = replay_from_audit(service.storage, study_id)
        if role != ROLE_ADMIN:
            report.pop("master_seed", None)
        return envelope(report, f"/v1/studies/{study_id}/replay", None)

    # --------------------------------------------------------------- outcomes

    @app.post("/v1/studies/{study_id}/outcomes", tags=["outcomes"])
    async def record_outcome(
        study_id: str,
        body: OutcomeRequest,
        role: str = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, Any]:
        service.storage.record_outcome(study_id, body.subject_id, body.y)
        return envelope(
            {"study_id": study_id, "subject_id": body.subject_id, "recorded": True},
            f"/v1/studies/{study_id}/outcomes", None,
        )

    @app.get("/v1/studies/{study_id}/effect", tags=["outcomes"])
    async def effect(
        study_id: str,
        arm_a: str | None = None,
        arm_b: str | None = None,
        role: str = Depends(require_role(ROLE_AUDITOR, ROLE_ADMIN)),
    ) -> dict[str, Any]:
        cfg, _ = service.storage.get_study(study_id)
        if arm_a is None or arm_b is None:
            if len(cfg.arms) != 2:
                raise AllocationError(
                    ErrorCategory.VALIDATION_ERROR,
                    "arm_a and arm_b query parameters are required for >2-arm studies",
                    {"arms": list(cfg.arms)},
                    http_status=422,
                )
            arm_a, arm_b = cfg.arms[0], cfg.arms[1]
        grouped = service.storage.outcomes(study_id)
        missing = [a for a in (arm_a, arm_b) if a not in grouped or not grouped[a]]
        if missing:
            raise AllocationError(
                ErrorCategory.INDETERMINATE,
                "no outcomes recorded for one or both requested arms",
                {"arms_without_outcomes": missing},
                http_status=409,
            )
        report = build_effect_report(
            arm_a, [v for _, v in grouped[arm_a]],
            arm_b, [v for _, v in grouped[arm_b]],
            permutation_seed=service.config.master_seed & 0x7FFFFFFF,
        )
        return envelope(report, f"/v1/studies/{study_id}/effect", None)

    return app


def _public_config(
    cfg: Any, include_seed: bool, master_seed: int
) -> dict[str, Any]:
    data = {
        "study_id": cfg.study_id,
        "arms": list(cfg.arms),
        "stratification_factors": list(cfg.stratification_factors),
        "block_sizes": list(cfg.block_sizes),
        "allocation_ratio": list(cfg.allocation_ratio),
        "tail_policy": cfg.tail_policy.value,
        "stream_scheme": "stratblock-deterministic-rng-v1",
        "contract_version": __version__,
    }
    if include_seed:
        data["master_seed"] = master_seed
    return data


def _redact_seed(report: dict[str, Any]) -> dict[str, Any]:
    provenance = report.get("stream_provenance")
    if isinstance(provenance, dict) and "master_seed" in provenance:
        provenance["master_seed"] = "redacted (administrator role only)"
    return report


def main() -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(
        "stratblock.api:create_app",
        factory=True,
        host="127.0.0.1",
        port=8080,
        log_config=None,
    )


if __name__ == "__main__":
    main()
