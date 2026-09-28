"""FastAPI application factory and HTTP routes."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .. import __version__
from ..corpus.errors import CycleError, SpecError
from ..index.repository import IndexRepository
from ..index.service import IndexService
from ..config import SETTINGS, Settings
from ..logging_setup import configure_logging
from .schemas import (
    CorpusSummary,
    ErrorResponse,
    LoadReportResponse,
    ModelSummary,
    OutputItem,
    QueryRequest,
    QueryResponse,
)
from .service import QueryService


def create_app(
    *,
    settings: Settings | None = None,
    repository: IndexRepository | None = None,
) -> FastAPI:
    settings = settings or SETTINGS
    configure_logging(settings.log_level)
    logger = logging.getLogger("wfst_service")

    db_path = settings.db_path
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    repo = repository or IndexRepository(db_path)
    index_service = IndexService(repo)
    query_service = QueryService(index_service, settings)

    app = FastAPI(
        title="Small weighted-FST service",
        version=__version__,
        description=(
            "Lexicon mapping, epsilon-filtered composition and bounded "
            "shortest-output enumeration."
        ),
    )
    app.state.settings = settings
    app.state.repository = repo
    app.state.query_service = query_service
    app.state.index_service = index_service

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service": "wfst-service",
            "version": __version__,
            "db": db_path,
        }

    @app.get("/version")
    def version() -> dict:
        return {"version": __version__, "schema": "wfst.corpus.v1"}

    @app.post("/corpora/{corpus_id}/load", response_model=LoadReportResponse)
    def load_corpus(corpus_id: str, payload: dict) -> LoadReportResponse:
        """Load a corpus from an inline JSON document."""
        try:
            report = index_service.load_corpus_json(corpus_id, payload)
        except SpecError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "spec_error",
                    "message": str(exc),
                    "details": {"path": exc.path} if exc.path else None,
                },
            ) from exc
        except CycleError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "cycle_error",
                    "message": str(exc),
                    "details": {"witness": list(exc.states)} if exc.states else None,
                },
            ) from exc
        logger.info(
            f"corpus loaded id={corpus_id} models={len(report.models)}",
            extra={"corpus_id": corpus_id, "event": "corpus_loaded"},
        )
        return LoadReportResponse(
            corpus_id=report.corpus_id,
            models=[
                ModelSummary(
                    name=m.name,
                    kind=m.kind,
                    num_states=m.num_states,
                    num_arcs=m.num_arcs,
                )
                for m in report.models
            ],
            pipelines=list(report.pipelines),
            logs=list(report.composition_logs),
        )

    @app.get("/corpora", response_model=list[CorpusSummary])
    def list_corpora() -> list[CorpusSummary]:
        summaries: list[CorpusSummary] = []
        for row in repo.list_corpora():
            summaries.append(
                CorpusSummary(
                    corpus_id=row["corpus_id"],
                    description=row["description"],
                    loaded_at=row["loaded_at"],
                    transducers=[
                        ModelSummary(**t)
                        for t in repo.list_transducers(row["corpus_id"])
                    ],
                    pipelines={
                        name: info["sequence"]
                        for name, info in repo.list_pipelines(
                            row["corpus_id"]
                        ).items()
                    },
                )
            )
        return summaries

    @app.get(
        "/corpora/{corpus_id}/runs/{run_id}",
        response_model=None,
    )
    def get_run(corpus_id: str, run_id: str) -> JSONResponse:
        run = repo.get_run(run_id)
        if run is None or run.corpus_id != corpus_id:
            return JSONResponse(
                status_code=404,
                content=ErrorResponse(
                    error_code="not_found",
                    message=f"run {run_id!r} not found in corpus "
                            f"{corpus_id!r}",
                ).model_dump(),
            )
        return JSONResponse(
            status_code=200,
            content={
                "run_id": run.run_id,
                "target": run.target,
                "input": run.input,
                "status": run.status,
                "complete": run.complete,
                "expansions": run.expansions,
                "error_code": run.error_code,
                "error_message": run.error_message,
                "results": repo.get_results(run_id),
                "logs": repo.get_logs(run_id),
            },
        )

    @app.post("/query", response_model=QueryResponse)
    def query(request: QueryRequest) -> QueryResponse:
        outcome = query_service.execute(
            corpus_id=request.corpus_id,
            target=request.target,
            text=request.input,
            k=request.k,
            budget=request.budget,
        )

        if outcome.status == "budget_exhausted":
            # Incomplete result: explicit 503, never a success envelope.
            return JSONResponse(  # type: ignore[return-value]
                status_code=503,
                content=_error_body(outcome, request),
            )
        if outcome.status == "error":
            code = outcome.error_code or "error"
            status_code = 404 if code == "not_found" else 422
            return JSONResponse(  # type: ignore[return-value]
                status_code=status_code,
                content=_error_body(outcome, request),
            )

        result = outcome.result
        assert result is not None
        # Annotate lexicographic ranks inside equal-cost groups.
        groups: dict[float, list[str]] = {}
        for item in result.outputs:
            groups.setdefault(item.cost, []).append(item.output)
        lex_index = {
            cost: {out: idx for idx, out in enumerate(sorted(outputs))}
            for cost, outputs in groups.items()
        }
        outputs = [
            OutputItem(
                rank=rank,
                output=item.output,
                cost=item.cost,
                lex_rank=lex_index[item.cost][item.output],
            )
            for rank, item in enumerate(result.outputs, start=1)
        ]
        decision = (
            "accepted: at least one output; ordering = (total cost asc, "
            "output lexicographic asc); outputs deduplicated at minimum "
            "alignment cost"
            if result.accepted
            else "rejected:no_path: start cannot reach any co-final state "
                 "for this input on the resolved model"
        )
        return QueryResponse(
            run_id=outcome.run_id,
            corpus_id=request.corpus_id,
            target=request.target,
            input=request.input,
            version=__version__,
            status=outcome.status,
            complete=result.complete,
            accepted=result.accepted,
            k=request.k,
            budget=request.budget,
            expansions=result.expansions,
            budget_used_fraction=round(
                result.expansions / max(request.budget, 1), 6
            ),
            outputs=outputs,
            decision=decision,
            logs=list(outcome.logs),
        )

    return app


def _error_body(outcome, request: QueryRequest) -> dict:
    body = ErrorResponse(
        error_code=outcome.error_code or "error",
        message=outcome.error_message or "unknown failure",
        run_id=outcome.run_id,
    ).model_dump()
    body["version"] = __version__
    body["input"] = request.input
    body["target"] = request.target
    body["complete"] = False
    body["logs"] = list(outcome.logs)
    return body
