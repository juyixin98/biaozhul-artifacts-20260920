"""Service layer: orchestrates language, kernel and oracle.

The HTTP layer stays thin; all business behavior lives here. Every response
is built with: request identity, engine version, the key processing steps
(and where they ran), a separate list of failure categories and a separate
list for uncertain/non-evaluated conclusions.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .. import ENGINE_VERSION
from ..kernel import reason
from ..language import LanguageRejection, Ontology, parse_ontology
from ..oracle import evaluate
from ..store import EvidenceStore
from .serialization import result_to_dict

logger = logging.getLogger("owl.service")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReasoningService:
    def __init__(self, store: EvidenceStore, *, model_cap: int = 100_000) -> None:
        self.store = store
        self.model_cap = model_cap

    # ------------------------------------------------------------------
    def classify(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        endpoint: str = "POST /reason",
        cross_check: bool = True,
    ) -> dict[str, Any]:
        request_id = request_id or f"req_{uuid4().hex[:16]}"
        started = time.perf_counter()
        received_at = _now()
        steps: list[dict[str, str]] = []

        def record(stage: str, location: str, detail: str) -> None:
            logger.info("[%s] %s @ %s :: %s", request_id, stage, location, detail)
            steps.append({"stage": stage, "location": location, "detail": detail})

        try:
            record("parse", "app.language.parse_ontology", "validating restricted syntax")
            onto: Ontology = parse_ontology(payload)
            record("parse", "app.language.parse_ontology",
                   f"accepted {len(onto.axioms)} axioms")

            record("classify", "app.kernel.reason", "forward-chaining closure")
            result = reason(onto, engine_version=ENGINE_VERSION)
            record("classify", "app.kernel.reason",
                   f"consistent={result.consistent}; "
                   f"unsatisfiable_classes={len(result.unsatisfiable)}; "
                   f"instances={len(result.instances)}")

            uncertainty: list[str] = []
            cross = None
            if cross_check:
                record("cross_check", "app.oracle.evaluate",
                       "enumerating finite models (independent implementation)")
                try:
                    verdict = evaluate(onto, model_cap=self.model_cap)
                except ValueError as exc:
                    uncertainty.append(
                        f"oracle cross-check skipped: {exc}"
                    )
                    record("cross_check", "app.oracle.evaluate", str(exc))
                else:
                    cross = self._agreement(result, verdict)
                    record("cross_check", "app.oracle.evaluate",
                           f"agreement={cross['agreement']} "
                           f"models={verdict.model_count}")
                    if not cross["agreement"]:
                        uncertainty.append(
                            "kernel and independent oracle disagree; see "
                            "cross_check.discrepancies"
                        )

            body = result_to_dict(result)
            response = {
                "request_id": request_id,
                "engine_version": ENGINE_VERSION,
                "received_at": received_at,
                "status": "ok",
                "consistent": result.consistent,
                "failure_categories": list(result.failure_categories),
                "uncertainties": uncertainty,
                "result": body,
                "cross_check": cross,
                "processing_steps": steps,
            }
            duration = (time.perf_counter() - started) * 1000
            self.store.save_request(
                request_id=request_id,
                received_at=received_at,
                engine_version=ENGINE_VERSION,
                endpoint=endpoint,
                status="ok",
                consistent=result.consistent,
                failure_categories=list(result.failure_categories),
                payload=payload,
                result=body,
                duration_ms=duration,
            )
            self.store.save_steps(request_id, steps)
            return response

        except LanguageRejection as exc:
            duration = (time.perf_counter() - started) * 1000
            logger.warning("[%s] rejected %s @ %s :: %s",
                           request_id, exc.code, exc.path, exc)
            steps.append({
                "stage": "reject",
                "location": "app.language",
                "detail": f"{exc.code} at {exc.path}: {exc}",
            })
            self.store.save_request(
                request_id=request_id,
                received_at=received_at,
                engine_version=ENGINE_VERSION,
                endpoint=endpoint,
                status="rejected",
                consistent=None,
                failure_categories=["UNSUPPORTED_CONSTRUCT"],
                payload=payload,
                result=None,
                duration_ms=duration,
                error_code=exc.code,
                error_path=exc.path,
                error_message=str(exc),
            )
            self.store.save_steps(request_id, steps)
            return {
                "request_id": request_id,
                "engine_version": ENGINE_VERSION,
                "received_at": received_at,
                "status": "rejected",
                "failure_categories": ["UNSUPPORTED_CONSTRUCT"],
                "uncertainties": [],
                "error": {
                    "code": exc.code,
                    "path": exc.path,
                    "message": str(exc),
                },
                "processing_steps": steps,
            }

    # ------------------------------------------------------------------
    def subsumption_query(
        self,
        payload: dict[str, Any],
        *,
        sub: str,
        sup: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or f"req_{uuid4().hex[:16]}"
        received_at = _now()
        started = time.perf_counter()
        steps: list[dict[str, str]] = []
        try:
            onto = parse_ontology(payload)
            result = reason(onto, engine_version=ENGINE_VERSION)
            entailed = result.is_subclass(sub, sup)
            steps.append({
                "stage": "subsumption",
                "location": "app.kernel.ReasoningResult.is_subclass",
                "detail": f"{sub} <= {sup} entailed={entailed}",
            })
            reason_detail = (
                "vacuously true (subclass is unsatisfiable)"
                if entailed and sub in result.unsatisfiable_nodes
                else "entailed by closure" if entailed
                else "not entailed"
            )
            response = {
                "request_id": request_id,
                "engine_version": ENGINE_VERSION,
                "received_at": received_at,
                "status": "ok",
                "consistent": result.consistent,
                "failure_categories": list(result.failure_categories),
                "uncertainties": [],
                "query": {"sub": sub, "super": sup},
                "entailed": entailed,
                "explanation": reason_detail,
                "processing_steps": steps,
            }
            self.store.save_request(
                request_id=request_id,
                received_at=received_at,
                engine_version=ENGINE_VERSION,
                endpoint="POST /subclass",
                status="ok",
                consistent=result.consistent,
                failure_categories=list(result.failure_categories),
                payload=payload,
                result={"entailed": entailed, "sub": sub, "super": sup},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            self.store.save_steps(request_id, steps)
            return response
        except LanguageRejection as exc:
            return self._rejection_response(
                exc, payload, request_id, received_at, started,
                "POST /subclass", steps,
            )

    # ------------------------------------------------------------------
    def _rejection_response(
        self, exc, payload, request_id, received_at, started, endpoint, steps
    ) -> dict[str, Any]:
        logger.warning("[%s] rejected %s @ %s", request_id, exc.code, exc.path)
        steps.append({
            "stage": "reject", "location": "app.language",
            "detail": f"{exc.code} at {exc.path}: {exc}",
        })
        self.store.save_request(
            request_id=request_id,
            received_at=received_at,
            engine_version=ENGINE_VERSION,
            endpoint=endpoint,
            status="rejected",
            consistent=None,
            failure_categories=["UNSUPPORTED_CONSTRUCT"],
            payload=payload,
            result=None,
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code=exc.code,
            error_path=exc.path,
            error_message=str(exc),
        )
        self.store.save_steps(request_id, steps)
        return {
            "request_id": request_id,
            "engine_version": ENGINE_VERSION,
            "received_at": received_at,
            "status": "rejected",
            "failure_categories": ["UNSUPPORTED_CONSTRUCT"],
            "uncertainties": [],
            "error": {"code": exc.code, "path": exc.path, "message": str(exc)},
            "processing_steps": steps,
        }

    # ------------------------------------------------------------------
    def _agreement(self, result, verdict) -> dict[str, Any]:
        discrepancies: list[str] = []
        kernel_unsat = set(result.unsatisfiable_nodes)
        oracle_unsat = set(verdict.unsatisfiable_nodes)
        if kernel_unsat != oracle_unsat:
            discrepancies.append(
                f"unsatisfiable_nodes differ: kernel_only={sorted(kernel_unsat - oracle_unsat)}, "
                f"oracle_only={sorted(oracle_unsat - kernel_unsat)}"
            )
        if result.consistent != verdict.consistent:
            discrepancies.append(
                f"consistency differs: kernel={result.consistent} oracle={verdict.consistent}"
            )
        return {
            "agreement": not discrepancies,
            "oracle_model_count": verdict.model_count,
            "oracle_variable_count": verdict.variable_count,
            "discrepancies": discrepancies,
        }
