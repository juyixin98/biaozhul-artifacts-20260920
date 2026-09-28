"""HTTP routes. Errors are reported with explicit categories; failures are
never returned as successes."""

from __future__ import annotations

import platform
import sqlite3

import fastapi
import pydantic
from fastapi import APIRouter, Request

from app import APP_VERSION
from app.api.validation import MineRequest
from app.corpus.schema import CorpusSpec
from app.corpus.store import corpus_digest
from app.mining.constraints import MiningConstraintError
from app.mining.kernel import PrefixGrowthMiner
from app.models.domain import GapConstraints

router = APIRouter()


class NotFoundError(Exception):
    def __init__(self, resource: str, identifier: str):
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"{resource} not found: {identifier}")


def component_versions() -> dict:
    return {
        "app": APP_VERSION,
        "python": platform.python_version(),
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
        "sqlite": sqlite3.sqlite_version,
    }


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/meta")
def meta() -> dict:
    return {"versions": component_versions()}


@router.post("/corpora", status_code=201)
def create_corpus(spec: CorpusSpec, request: Request) -> dict:
    store = request.app.state.store
    corpus_id, digest = store.save_corpus(spec)
    return {
        "corpus_id": corpus_id,
        "digest": digest,
        "name": spec.name,
        "sequence_count": len(spec.sequences),
        "event_count": sum(len(s.events) for s in spec.sequences),
        "has_timestamps": spec.has_timestamps,
    }


@router.get("/corpora/{corpus_id}")
def get_corpus(corpus_id: str, request: Request) -> dict:
    store = request.app.state.store
    spec = store.get_corpus_spec(corpus_id)
    if spec is None:
        raise NotFoundError("corpus", corpus_id)
    return spec


@router.post("/mine", status_code=201)
def mine(body: MineRequest, request: Request) -> dict:
    store = request.app.state.store
    settings = request.app.state.settings
    logger = request.app.state.logger

    if not store.corpus_exists(body.corpus_id):
        raise NotFoundError("corpus", body.corpus_id)

    sequences = store.load_sequences(body.corpus_id)
    constraints = GapConstraints(
        max_pos_gap=body.max_pos_gap, max_time_gap=body.max_time_gap
    )
    params = body.model_dump()
    versions = component_versions()
    run_id = store.create_run(body.corpus_id, params, versions)
    log_extra = {
        "run_id": run_id,
        "corpus_id": body.corpus_id,
        "corpus_digest": corpus_digest(CorpusSpec(**store.get_corpus_spec(body.corpus_id))),
    }
    logger.info(
        "run_created",
        extra={**log_extra, "step": "run_created", "detail": params, "versions": versions},
    )
    try:
        miner = PrefixGrowthMiner(
            sequences,
            constraints,
            min_support=body.min_support,
            max_pattern_len=body.max_pattern_len or settings.max_pattern_len,
            max_embeddings_per_sequence=settings.max_embeddings_per_sequence,
            logger=logger,
            run_id=run_id,
        )
        results = miner.mine()
    except MiningConstraintError as exc:
        store.finish_run(run_id, "FAILED", error=f"constraint_error: {exc}")
        logger.warning("run_failed", extra={**log_extra, "step": "run_failed", "detail": str(exc)})
        raise
    except Exception as exc:
        store.finish_run(run_id, "FAILED", error=f"internal: {exc}")
        logger.error("run_failed", exc_info=exc,
                     extra={**log_extra, "step": "run_failed", "detail": str(exc)})
        raise

    store.save_patterns(run_id, results)
    store.finish_run(run_id, "COMPLETED", pattern_count=len(results))
    logger.info(
        "run_completed",
        extra={**log_extra, "step": "run_completed",
               "detail": {"pattern_count": len(results)}},
    )
    return {
        "run_id": run_id,
        "status": "COMPLETED",
        "pattern_count": len(results),
        "patterns": [
            {
                "pattern": list(r.pattern),
                "support": r.support,
                "embeddings": [
                    {"sequence_id": e.sequence_id, "positions": list(e.positions)}
                    for e in r.embeddings
                ],
                "evidence_complete": r.evidence_complete,
            }
            for r in results
        ],
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    store = request.app.state.store
    run = store.get_run(run_id)
    if run is None:
        raise NotFoundError("run", run_id)
    return run


@router.get("/runs/{run_id}/patterns")
def get_run_patterns(run_id: str, request: Request) -> dict:
    store = request.app.state.store
    run = store.get_run(run_id)
    if run is None:
        raise NotFoundError("run", run_id)
    return {
        "run_id": run_id,
        "status": run["status"],
        "patterns": store.list_patterns(run_id),
    }
