"""FastAPI application: explainable, request-correlated query interface.

Every response (success or error) carries the same ``request_id``; the same id
appears in structured log records and in the persisted ``request_log`` table,
joining API output -> staged processing steps -> stored evidence.

Failure reasons and uncertain conclusions are reported in dedicated sections,
never mixed into the positive results.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse

from ..config import ENGINE_VERSION, get_settings
from ..lang import ast
from ..lang.errors import ErrorCode, LangError
from ..lang.parser import axiom_from_json, parse_axioms
from ..service.explain import shape_result
from ..service.reasoning import ReasoningService, conflict_to_dict, proof_to_dict
from ..store.evidence import EvidenceStore
from .schemas import CreateOntology

logger = logging.getLogger("miniowl")

_REQUEST_ID_HEADER = "x-request-id"


# --------------------------------------------------------------------------- #
# Assembling axioms from the two accepted input surfaces
# --------------------------------------------------------------------------- #
def _build_axioms(payload: CreateOntology) -> list[tuple[str, ast.Axiom]]:
    items: list[tuple[str, ast.Axiom]] = []
    for idx, node in enumerate(payload.axioms):
        axiom = axiom_from_json(node, idx)
        items.append((f"a{idx + 1:03d}", axiom))
    if payload.functional:
        offset = len(items)
        for j, axiom in enumerate(parse_axioms(payload.functional)):
            items.append((f"a{offset + j + 1:03d}", axiom))
    return items


def _axiom_storage_rows(
    items: list[tuple[str, ast.Axiom]]
) -> list[tuple[str, int, str, str, dict[str, Any]]]:
    from ..lang.compiler import render_expr

    rows: list[tuple[str, int, str, str, dict[str, Any]]] = []
    for idx, (aid, axiom) in enumerate(items):
        if isinstance(axiom, ast.SubClassOf):
            kind, text = "SubClassOf", f"SubClassOf({render_expr(axiom.sub)} {render_expr(axiom.sup)})"
        elif isinstance(axiom, ast.EquivalentClasses):
            kind = "EquivalentClasses"
            text = "EquivalentClasses(" + " ".join(render_expr(o) for o in axiom.operands) + ")"
        elif isinstance(axiom, ast.DisjointClasses):
            kind = "DisjointClasses"
            text = "DisjointClasses(" + " ".join(render_expr(o) for o in axiom.operands) + ")"
        else:
            kind = "ClassAssertion"
            text = f"ClassAssertion({render_expr(axiom.cls)} {axiom.individual})"
        rows.append((aid, idx, kind, text, {"kind": kind, "text": text}))
    return rows


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
class _RequestIdFilter(logging.Filter):
    """Guarantee request_id exists on every record, so third-party loggers
    (uvicorn/httpx) that do not set it never crash our formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


def _configure_logging(level: str) -> None:
    # Attach to the miniowl logger only -- never to root -- so the
    # request_id-bearing format cannot be applied to foreign log records.
    package_logger = logging.getLogger("miniowl")
    if not package_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s miniowl [request_id=%(request_id)s] %(message)s"
            )
        )
        handler.addFilter(_RequestIdFilter())
        package_logger.addHandler(handler)
    package_logger.setLevel(level)
    package_logger.propagate = False


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.store = EvidenceStore(settings.db_path)
        app.state.service = ReasoningService()
        yield

    app = FastAPI(
        title="Restricted OWL Class-Expression Service",
        version="1.0.0",
        description="Subclass / equivalence / disjointness / intersection only; unsupported OWL constructs are rejected explicitly.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        request_id = request.headers.get(_REQUEST_ID_HEADER) or f"req-{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id
        start = time.perf_counter()
        handler = logging.LoggerAdapter(logger, {"request_id": request_id})
        request.state.log = handler
        store: EvidenceStore = request.app.state.store if hasattr(request.app.state, "store") else None

        try:
            if store is not None:
                store.log_request(request_id, request.method, request.url.path, "received")
            handler.info("request received %s %s", request.method, request.url.path)
            response = await call_next(request)
        except Exception:
            if store is not None:
                store.log_request(request_id, request.method, request.url.path, "unhandled_exception", 500)
            raise
        elapsed = (time.perf_counter() - start) * 1000
        response.headers[_REQUEST_ID_HEADER] = request_id
        response.headers["x-engine-version"] = ENGINE_VERSION
        if store is not None:
            store.log_request(
                request_id, request.method, request.url.path, "completed",
                response.status_code, {"elapsed_ms": round(elapsed, 2)},
            )
        handler.info("request completed status=%s elapsed_ms=%.2f", response.status_code, elapsed)
        return response

    def _error(status: int, code: str, message: str, request_id: str, **extra: Any) -> JSONResponse:
        body = {"error": {"code": code, "message": message, **extra}, "request_id": request_id}
        return JSONResponse(status_code=status, content=body, headers={_REQUEST_ID_HEADER: request_id})

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        return {"status": "ok", "engine_version": ENGINE_VERSION, "request_id": request.state.request_id}

    @app.post("/ontologies", status_code=201)
    async def create_ontology(payload: CreateOntology, request: Request) -> JSONResponse:
        rid = request.state.request_id
        store: EvidenceStore = request.app.state.store
        if not payload.axioms and not payload.functional:
            return _error(422, ErrorCode.MALFORMED_EXPRESSION, "provide at least one axiom", rid)
        try:
            items = _build_axioms(payload)
        except LangError as exc:
            store.log_request(rid, "POST", "/ontologies", "rejected", 422, exc.to_dict())
            return _error(422, exc.code, exc.message, rid, position=exc.position)

        ontology_id = payload.ontology_id or f"onto-{uuid.uuid4().hex[:12]}"
        if store.ontology_exists(ontology_id):
            return _error(409, ErrorCode.CONFLICT, f"ontology {ontology_id!r} already exists", rid)

        rows = _axiom_storage_rows(items)
        store.save_ontology(ontology_id, payload.label, rows)
        store.log_request(rid, "POST", f"/ontologies/{ontology_id}", "stored", 201,
                          {"axioms": len(items), "ontology_id": ontology_id})
        request.state.log.info("stored ontology %s with %d axioms", ontology_id, len(items))
        return JSONResponse(
            status_code=201,
            content={"ontology_id": ontology_id, "axiom_count": len(items),
                     "axioms": [{"axiom_id": aid, "text": text} for aid, _, _, text, _ in rows],
                     "request_id": rid},
            headers={_REQUEST_ID_HEADER: rid},
        )

    @app.get("/ontologies")
    async def list_ontologies(request: Request) -> dict[str, Any]:
        return {"ontologies": request.app.state.store.list_ontologies(),
                "request_id": request.state.request_id}

    @app.get("/ontologies/{ontology_id}")
    async def get_ontology(ontology_id: str, request: Request) -> Any:
        store: EvidenceStore = request.app.state.store
        if not store.ontology_exists(ontology_id):
            return _error(404, ErrorCode.NOT_FOUND, f"ontology {ontology_id!r} not found", request.state.request_id)
        rows = store.load_axioms(ontology_id)
        meta = [o for o in store.list_ontologies() if o["ontology_id"] == ontology_id][0]
        return {
            **meta,
            "axioms": [{"axiom_id": r.axiom_id, "kind": r.kind, "text": r.functional_text} for r in rows],
            "request_id": request.state.request_id,
        }

    @app.post("/ontologies/{ontology_id}/reason")
    async def reason(
        request: Request,
        ontology_id: str = Path(...),
        include_oracle: bool = Query(True),
        persist: bool = Query(True),
    ) -> Any:
        rid = request.state.request_id
        store: EvidenceStore = request.app.state.store
        svc: ReasoningService = request.app.state.service
        if not store.ontology_exists(ontology_id):
            return _error(404, ErrorCode.NOT_FOUND, f"ontology {ontology_id!r} not found", rid)

        rows = store.load_axioms(ontology_id)
        # Re-parse from stored functional text: source declarations are authoritative.
        try:
            parsed = parse_axioms("\n".join(r.functional_text for r in rows))
        except LangError as exc:  # pragma: no cover - stored text parsed once already
            return _error(422, exc.code, exc.message, rid, position=exc.position)
        items = list(zip([r.axiom_id for r in rows], parsed, strict=True))

        store.log_request(rid, "POST", f"/ontologies/{ontology_id}/reason", "compile_start")
        program = svc.compile(items)
        report, groups, duration_ms = svc.reason(program)

        oracle_skipped: str | None = None
        cross_check: CrossCheck | None = None
        if include_oracle:
            axiom_nodes = [a for _, a in items]
            try:
                _, cross_check = svc.oracle_check(
                    axiom_nodes, report, max_classes=settings.max_oracle_classes
                )
            except ValueError as exc:
                oracle_skipped = str(exc)

        result = shape_result(
            ontology_id=ontology_id,
            program=program,
            report=report,
            groups=groups,
            cross_check=cross_check,
            duration_ms=duration_ms,
            oracle_skipped=oracle_skipped,
        )

        if persist:
            run_id = f"run-{uuid.uuid4().hex[:12]}"
            store.save_run(
                run_id, ontology_id, ENGINE_VERSION, duration_ms,
                report.inconsistent, [u.cls for u in report.unsatisfiable], result,
            )
            result["run_id"] = run_id
        result["request_id"] = rid
        store.log_request(
            rid, "POST", f"/ontologies/{ontology_id}/reason",
            "reasoned" if (cross_check is None or cross_check.agree) else "cross_check_mismatch",
            200,
            {"inconsistent": report.inconsistent,
             "unsatisfiable": [u.cls for u in report.unsatisfiable]},
        )
        return result

    @app.get("/ontologies/{ontology_id}/runs")
    async def list_runs(ontology_id: str, request: Request) -> Any:
        store: EvidenceStore = request.app.state.store
        if not store.ontology_exists(ontology_id):
            return _error(404, ErrorCode.NOT_FOUND, f"ontology {ontology_id!r} not found", request.state.request_id)
        return {"ontology_id": ontology_id, "runs": store.list_runs(ontology_id),
                "request_id": request.state.request_id}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> Any:
        report = request.app.state.store.load_run_report(run_id)
        if report is None:
            return _error(404, ErrorCode.NOT_FOUND, f"run {run_id!r} not found", request.state.request_id)
        return report

    @app.get("/requests/{request_id}")
    async def get_request_events(request_id: str, request: Request) -> dict[str, Any]:
        return {"request_id": request_id, "events": request.app.state.store.request_events(request_id)}

    @app.post("/ontologies/{ontology_id}/query")
    async def query_individual(
        request: Request,
        ontology_id: str,
        individual: str = Query(..., description="individual name to explain"),
    ) -> Any:
        """Explain one individual: entailed types with proofs, or conflict path."""
        rid = request.state.request_id
        store: EvidenceStore = request.app.state.store
        svc: ReasoningService = request.app.state.service
        if not store.ontology_exists(ontology_id):
            return _error(404, ErrorCode.NOT_FOUND, f"ontology {ontology_id!r} not found", rid)
        rows = store.load_axioms(ontology_id)
        parsed = parse_axioms("\n".join(r.functional_text for r in rows))
        program = svc.compile(list(zip([r.axiom_id for r in rows], parsed, strict=True)))
        report, _, _ = svc.reason(program)
        r = report.individual(individual)
        if r is None:
            declared = sorted(program.individuals)
            return _error(404, ErrorCode.UNKNOWN_INDIVIDUAL,
                          f"individual {individual!r} has no ClassAssertion", rid,
                          declared_individuals=declared)
        body: dict[str, Any] = {
            "request_id": rid,
            "engine_version": ENGINE_VERSION,
            "individual": individual,
            "entailed_types": sorted(r.types),
            "proofs": {p: proof_to_dict(n) for p, n in sorted(r.proofs.items())},
        }
        if r.conflict is not None:
            body["failure"] = conflict_to_dict(r.conflict)
        return body

    return app


app = create_app()
