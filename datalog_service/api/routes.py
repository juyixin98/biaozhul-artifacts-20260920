"""Route handlers for the Datalog service.

Endpoints are synchronous ``def`` functions because they call the blocking
SQLite store / fixpoint engine; Starlette runs them in its worker thread
pool.  All shared collaborators live in :class:`ServiceContext`, attached
to the application at construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import APIRouter, Request

from ..config import Settings
from ..language.ast import format_goal
from ..language.errors import ParseError, QueryError, StateError
from ..query.answering import answer_query, goal_variables
from ..service import DatalogService
from ..storage.evidence_store import EvidenceStore
from .schemas import PredicateInfoModel, QueryRequest, SubmitRequest

router = APIRouter()


@dataclass(frozen=True)
class ServiceContext:
    store: EvidenceStore
    service: DatalogService
    settings: Settings


def ctx_of(request: Request) -> ServiceContext:
    return request.app.state.ctx


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _log(request: Request, endpoint: str, status: str, http_status: int,
         **fields: Any) -> None:
    ctx_of(request).store.log_request(
        request_id=request.state.request_id,
        endpoint=endpoint,
        status=status,
        http_status=http_status,
        **fields,
    )


def _evaluation_block(mat: Any) -> Dict[str, Any]:
    return {
        "strata_rounds": list(mat.strata_rounds),
        "stats": [
            {"stratum": s.stratum, "rounds": s.rounds, "derived": s.derived}
            for s in mat.stats
        ],
        "trace": [t.to_dict() for t in mat.trace],
    }


def _predicates_block(compiled: Any) -> Dict[str, Any]:
    return {
        name: PredicateInfoModel(
            name=info.name,
            arity=info.arity,
            defined_by_facts=info.defined_by_facts,
            defined_by_rules=info.defined_by_rules,
            stratum=info.stratum,
        ).model_dump()
        for name, info in sorted(compiled.predicates.items())
    }


def _collect_uncertainty(answers: Any) -> list:
    """Proof leaves that are not plain facts are flagged, not hidden."""
    flagged: list = []
    for index, answer in enumerate(answers):
        stack = [answer.proof]
        while stack:
            node = stack.pop()
            if node.kind in ("unexplained", "depth_truncated"):
                flagged.append(
                    {
                        "answer_index": index,
                        "code": f"PROOF_{node.kind.upper()}",
                        "goal": format_goal(node.predicate, node.row, node.negated),
                        "message": (
                            "proof expansion was truncated at the depth limit"
                            if node.kind == "depth_truncated"
                            else "no derivation record found for this tuple"
                        ),
                    }
                )
            stack.extend(node.children)
    return flagged


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.get("/healthz")
def healthz(request: Request) -> Dict[str, str]:
    return {"status": "ok", "service": ctx_of(request).settings.service_name}


@router.post("/programs")
def submit_program(payload: SubmitRequest, request: Request) -> Dict[str, Any]:
    ctx = ctx_of(request)
    if len(payload.program) > ctx.settings.max_program_chars:
        raise ParseError(
            f"program exceeds {ctx.settings.max_program_chars} characters "
            f"(got {len(payload.program)})"
        )
    prepared = ctx.service.submit(payload.program)
    mat = prepared.materialization
    compiled = prepared.compiled
    _log(
        request, endpoint="POST /programs", status="ok", http_status=200,
        program_id=prepared.program_id,
        materialization_id=mat.materialization_id,
        result_count=len(compiled.rules),
    )
    return {
        "request_id": request.state.request_id,
        "status": "ok",
        "program_id": prepared.program_id,
        "materialization_id": mat.materialization_id,
        "rule_version": compiled.rule_version,
        "fact_set_version": mat.fact_set_version,
        "reused": prepared.reused,
        "rule_count": len(compiled.rules),
        "fact_count": len(prepared.facts),
        "strata": [[cr.rule_id for cr in group] for group in compiled.strata],
        "predicates": _predicates_block(compiled),
        "evaluation": _evaluation_block(mat),
        "failures": [],
    }


@router.post("/programs/{program_id}/query")
def run_query(program_id: str, payload: QueryRequest, request: Request) -> Dict[str, Any]:
    ctx = ctx_of(request)
    if len(payload.query) > ctx.settings.max_query_chars:
        raise QueryError(
            f"query exceeds {ctx.settings.max_query_chars} characters"
        )
    prepared = ctx.service.get(program_id)
    if prepared is None:
        raise StateError(f"unknown program_id {program_id!r}")
    request.state.goal = payload.query
    goal = ctx.service.parse_goal(payload.query)
    cap = payload.max_answers or ctx.settings.max_answers
    return _query_result(request, prepared, goal, payload, cap)


def _query_result(request: Request, prepared: Any, goal: Any,
                  payload: QueryRequest, cap: int) -> Dict[str, Any]:
    answers = answer_query(
        prepared.materialization, goal,
        include_proofs=payload.include_proofs, max_answers=cap + 1,
    )
    truncated = len(answers) > cap
    if truncated:
        answers = answers[:cap]
    uncertainty = _collect_uncertainty(answers)
    _log(
        request, endpoint="POST /programs/{id}/query", status="ok",
        http_status=200, program_id=prepared.program_id,
        materialization_id=prepared.materialization.materialization_id,
        goal=payload.query, result_count=len(answers),
        details={"truncated": truncated,
                 "uncertainty_count": len(uncertainty)},
    )
    return {
        "request_id": request.state.request_id,
        "status": "ok",
        "program_id": prepared.program_id,
        "materialization_id": prepared.materialization.materialization_id,
        "goal": goal.canonical() + "?",
        "variables": goal_variables(goal),
        "answer_count": len(answers),
        "truncated": truncated,
        "answers": [a.to_dict(payload.include_proofs) for a in answers],
        "rule_version": prepared.compiled.rule_version,
        "fact_set_version": prepared.materialization.fact_set_version,
        "failures": [],
        "uncertainty": uncertainty,
    }


@router.get("/programs/{program_id}")
def describe_program(program_id: str, request: Request) -> Dict[str, Any]:
    ctx = ctx_of(request)
    prepared = ctx.service.get(program_id)
    if prepared is None:
        return _describe_persisted(request, ctx, program_id)
    return _describe_live(request, prepared)


def _describe_persisted(request: Request, ctx: ServiceContext,
                        program_id: str) -> Dict[str, Any]:
    record = ctx.store.get_program(program_id)
    if record is None:
        raise StateError(f"unknown program_id {program_id!r}")
    _log(request, endpoint="GET /programs/{id}", status="ok",
         http_status=200, program_id=program_id)
    return {"request_id": request.state.request_id, "status": "ok",
            "program_id": program_id, "persisted": record}


def _describe_live(request: Request, prepared: Any) -> Dict[str, Any]:
    compiled = prepared.compiled
    mat = prepared.materialization
    _log(request, endpoint="GET /programs/{id}", status="ok", http_status=200,
         program_id=prepared.program_id, materialization_id=mat.materialization_id)
    return {
        "request_id": request.state.request_id,
        "status": "ok",
        "program_id": prepared.program_id,
        "materialization_id": mat.materialization_id,
        "rule_version": compiled.rule_version,
        "fact_set_version": mat.fact_set_version,
        "normalized_text": compiled.normalized_text,
        "rules": [
            {
                "rule_id": cr.rule_id,
                "rule_hash": cr.rule_hash,
                "stratum": compiled.predicates[cr.head_predicate].stratum,
                "text": cr.text,
            }
            for cr in compiled.rules
        ],
        "evaluation": {
            "strata_rounds": list(mat.strata_rounds),
            "trace": [t.to_dict() for t in mat.trace],
        },
    }


@router.get("/programs/{program_id}/tuples/{predicate}")
def list_tuples(program_id: str, predicate: str, request: Request) -> Dict[str, Any]:
    ctx = ctx_of(request)
    prepared = ctx.service.get(program_id)
    if prepared is None:
        raise StateError(f"unknown program_id {program_id!r}")
    if predicate not in prepared.compiled.predicates:
        raise QueryError(f"unknown predicate {predicate!r}")
    mat = prepared.materialization
    rows = ctx.store.list_tuple_rows(mat.materialization_id, predicate)
    _log(request, endpoint="GET /programs/{id}/tuples/{pred}",
         status="ok", http_status=200, program_id=program_id,
         materialization_id=mat.materialization_id, result_count=len(rows))
    return {
        "request_id": request.state.request_id,
        "status": "ok",
        "program_id": program_id,
        "predicate": predicate,
        "count": len(rows),
        "tuples": [
            {
                "row": json.loads(r["row_json"]),
                "kind": r["kind"],
                "rule_id": r["rule_id"],
                "round": r["round"],
                "stratum": r["stratum"],
            }
            for r in rows
        ],
    }


@router.get("/requests/{request_id}")
def get_request_log(request_id: str, request: Request) -> Dict[str, Any]:
    record = ctx_of(request).store.get_request(request_id)
    if record is None:
        raise StateError(f"unknown request_id {request_id!r}")
    return {"status": "ok", "request_id": request_id, "record": record}
