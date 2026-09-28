"""Query execution service: validation boundary + orchestration.

Responsibilities:

* validate the query (bounds from the configuration layer, single-char
  labels, known corpus/target) -- failures get explicit error codes;
* resolve the precomposed model from the index;
* run the bounded k-best search in the kernel;
* persist run metadata, ranked results and the correlated trace;
* map every kernel/index exception to a stable failure category -- an
  unknown/exceptional state is never reported as success.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

from .. import __version__
from ..corpus.errors import (
    BudgetExhausted,
    NotFoundError,
    QueryError,
    SpecError,
    WfstError,
)
from ..corpus.symbols import EPS_LITERAL
from ..core.errors import TopologyError
from ..core.search import KBestResult, SearchTrace, kbest
from ..config import Settings
from ..index.repository import IndexRepository
from ..index.service import IndexService

logger = logging.getLogger("wfst_service")


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    run_id: str
    result: KBestResult | None
    trace: SearchTrace | None
    status: str
    error_code: str | None = None
    error_message: str | None = None
    logs: tuple[str, ...] = field(default_factory=tuple)


class QueryService:
    def __init__(
        self,
        index_service: IndexService,
        settings: Settings,
    ) -> None:
        self._index = index_service
        self._settings = settings

    @property
    def repository(self) -> IndexRepository:
        return self._index.repository

    def execute(
        self,
        corpus_id: str,
        target: str,
        text: str,
        k: int | None = None,
        budget: int | None = None,
        *,
        run_id: str | None = None,
    ) -> QueryOutcome:
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        chosen_k = k if k is not None else self._settings.default_k
        chosen_budget = (
            budget if budget is not None else self._settings.default_budget
        )
        request_json = json.dumps(
            {
                "corpus_id": corpus_id,
                "target": target,
                "input": text,
                "k": chosen_k,
                "budget": chosen_budget,
            },
            ensure_ascii=False,
        )

        def fail(
            code: str,
            message: str,
            *,
            status: str = "error",
            persist: bool = True,
        ) -> QueryOutcome:
            logger.error(
                message,
                extra={
                    "run_id": run_id,
                    "corpus_id": corpus_id,
                    "target": target,
                    "input": text,
                    "event": f"query_failed:{code}",
                },
            )
            logs = (
                f"search run={run_id} fst={target!r} input={text!r} "
                f"k={chosen_k} budget={chosen_budget}",
                f"verdict: rejected:{code}",
                f"decision basis: request validation / resolution: {message}",
            )
            if persist:
                self._persist_failure(
                    run_id, corpus_id, target, text, chosen_k, chosen_budget,
                    request_json, code, message, logs,
                )
            return QueryOutcome(
                run_id=run_id,
                result=None,
                trace=None,
                status=status,
                error_code=code,
                error_message=message,
                logs=logs,
            )

        # ---- Validation (explicit failure categories) ------------------
        if not (1 <= len(corpus_id) <= 64):
            return fail("query_error", "corpus_id length out of range")
        if not (1 <= len(target) <= 128):
            return fail("query_error", "target length out of range")
        if not text or len(text) > self._settings.max_input_length:
            return fail(
                "query_error",
                f"input length must be in 1..{self._settings.max_input_length}",
            )
        if not (1 <= chosen_k <= self._settings.max_k):
            return fail(
                "query_error",
                f"k must be in 1..{self._settings.max_k}",
            )
        if not (1 <= chosen_budget <= self._settings.max_budget):
            return fail(
                "query_error",
                f"budget must be in 1..{self._settings.max_budget}",
            )
        if EPS_LITERAL in text:
            return fail(
                "query_error",
                f"input must not contain the reserved epsilon literal "
                f"{EPS_LITERAL!r}",
            )
        if any(ch.isspace() for ch in text):
            return fail("query_error", "input must not contain whitespace")

        # ---- Resolve model ---------------------------------------------
        try:
            fst, _kind = self._index.resolve_model(corpus_id, target)
        except NotFoundError as exc:
            return fail("not_found", str(exc), status="error")
        except WfstError as exc:
            return fail(exc.code, str(exc))

        # ---- Run search -------------------------------------------------
        self.repository.start_run(
            run_id, corpus_id, target, text, chosen_k, chosen_budget,
            request_json,
        )
        try:
            result, trace = kbest(
                fst, text, chosen_k,
                run_id=run_id, budget=chosen_budget,
            )
        except BudgetExhausted as exc:
            logs = (
                f"search run={run_id} fst={target!r} input={text!r} "
                f"k={chosen_k} budget={chosen_budget}",
                f"verdict: incomplete:budget_exhausted after "
                f"{exc.expansions} expansions (limit {exc.budget})",
                "decision basis: expansion budget spent before k outputs "
                "and tie set could be proven complete; result MUST NOT be "
                "treated as a successful answer",
            )
            logger.error(
                str(exc),
                extra={
                    "run_id": run_id, "corpus_id": corpus_id,
                    "target": target, "input": text,
                    "event": "budget_exhausted",
                },
            )
            self.repository.finish_run(
                run_id, "budget_exhausted", False, exc.expansions,
                error_code="budget_exhausted", error_message=str(exc),
            )
            self.repository.add_logs(run_id, list(logs))
            return QueryOutcome(
                run_id=run_id, result=None, trace=None,
                status="budget_exhausted",
                error_code="budget_exhausted",
                error_message=str(exc), logs=logs,
            )
        except (TopologyError, SpecError, WfstError) as exc:
            logs = (
                f"search run={run_id} fst={target!r} input={text!r}",
                f"verdict: rejected:{exc.code}: {exc}",
            )
            self.repository.finish_run(
                run_id, "error", False, 0,
                error_code=exc.code, error_message=str(exc),
            )
            self.repository.add_logs(run_id, list(logs))
            return QueryOutcome(
                run_id=run_id, result=None, trace=None, status="error",
                error_code=exc.code, error_message=str(exc), logs=logs,
            )

        # ---- Persist success / empty language ---------------------------
        logs = tuple(trace.as_lines())
        status = "ok" if result.accepted else "no_path"
        for rank, output in enumerate(result.outputs, start=1):
            self.repository.add_result(run_id, rank, output.output, output.cost)
        self.repository.add_logs(run_id, list(logs))
        self.repository.finish_run(
            run_id,
            status,
            complete=result.complete,
            expansions=result.expansions,
        )
        logger.info(
            f"query status={status} outputs={len(result.outputs)} "
            f"expansions={result.expansions} complete={result.complete}",
            extra={
                "run_id": run_id, "corpus_id": corpus_id,
                "target": target, "input": text, "event": "query_ok",
            },
        )
        return QueryOutcome(
            run_id=run_id,
            result=result,
            trace=trace,
            status=status,
            logs=logs,
        )

    def _persist_failure(
        self,
        run_id: str,
        corpus_id: str,
        target: str,
        text: str,
        k: int,
        budget: int,
        request_json: str,
        code: str,
        message: str,
        logs: tuple[str, ...],
    ) -> None:
        try:
            self.repository.start_run(
                run_id, corpus_id, target, text, k, budget, request_json
            )
            self.repository.finish_run(
                run_id, "error", False, 0,
                error_code=code, error_message=message,
            )
            self.repository.add_logs(run_id, list(logs))
        except Exception:  # pragma: no cover - persistence is best-effort
            logger.exception(
                "failed to persist rejected query",
                extra={"run_id": run_id, "event": "persist_failure"},
            )
