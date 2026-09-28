"""Application service: orchestrate compile -> evaluate -> answer -> record.

This is the layer the HTTP API calls.  It owns no inference logic of its
own; it wires the language, engine and store together and produces
explainable results:

* every response carries ``request_id`` and program ``version``;
* query answers include variable bindings and at least one verifiable
  proof tree;
* failures carry a stable ``error_category``;
* a per-request trace lists stratum/iteration steps and their locations;
* everything important is logged with the request id.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .engine.fixpoint import FixpointResult, evaluate
from .engine.proof import ProofBuilder
from .language.compiler import CompiledProgram, compile_program
from .language.errors import DatalogError, UnknownPredicateError
from .language.parser import Parser, ParseError
from .language.terms import Atom, Const
from .store.sqlite_store import EvidenceStore, new_request_id

log = logging.getLogger("datalog.service")


@dataclass
class QueryAnswer:
    bindings: dict[str, str]
    proof: dict
    uncertain: bool = False


@dataclass
class ServiceResponse:
    request_id: str
    status: str  # "ok" | "empty" | "error"
    version: str | None = None
    goal: str | None = None
    answers: list[QueryAnswer] = field(default_factory=list)
    strata: list[dict] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    error_category: str | None = None
    error_message: str | None = None
    uncertainty_notes: list[str] = field(default_factory=list)
    rule_texts: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "version": self.version,
            "goal": self.goal,
            "answer_count": len(self.answers),
            "answers": [
                {"bindings": a.bindings, "proof": a.proof, "uncertain": a.uncertain}
                for a in self.answers
            ],
            "strata": self.strata,
            "steps": self.steps,
            "uncertainty": self.uncertainty_notes,
            "failures": (
                [{"category": self.error_category, "message": self.error_message}]
                if self.error_category
                else []
            ),
            "rules": [{"index": i, "text": t} for i, t in enumerate(self.rule_texts)],
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


class DatalogService:
    def __init__(self, store: EvidenceStore, program_id: str = "default") -> None:
        self.store = store
        # Namespace prefix; the persisted id is namespaced per program
        # version so two different sources never share a programs row.
        self.program_id = program_id

    def _versioned_program_id(self, version: str) -> str:
        return f"{self.program_id}:{version}"

    # ------------------------------------------------------------------

    def run_query(
        self,
        source: str,
        goal_text: str,
        *,
        request_id: str | None = None,
        persist: bool = True,
    ) -> ServiceResponse:
        rid = request_id or new_request_id()
        started = time.perf_counter()
        logger = logging.LoggerAdapter(log, {"request_id": rid})
        resp = ServiceResponse(request_id=rid, status="error", goal=goal_text.strip() or None)
        try:
            logger.info("query start goal=%r", goal_text)
            compiled, result = self._compile_eval(source, resp, rid, logger, persist=persist)
            goal = self._parse_goal(goal_text, compiled)
            answers, notes = self._answer(goal, compiled, result)
            resp.answers = answers
            resp.uncertainty_notes = notes
            resp.status = "ok" if answers else "empty"
            if persist:
                self.store.record_request(
                    request_id=rid,
                    kind="query",
                    status=resp.status,
                    program_id=self._versioned_program_id(compiled.version),
                    version=compiled.version,
                    goal=goal.render(),
                    answer_count=len(answers),
                    uncertainty=bool(notes),
                )
            logger.info(
                "query done status=%s answers=%d strata=%d",
                resp.status,
                len(answers),
                len(compiled.strata),
            )
        except DatalogError as exc:
            resp.error_category = exc.category
            resp.error_message = str(exc)
            logger.warning("query failed category=%s: %s", exc.category, exc)
            if persist:
                self._safe_record_error(rid, goal_text, resp)
        except (ParseError, ValueError) as exc:
            # Parser raises ParseError(ValueError); normalise category.
            resp.error_category = "parse_error"
            resp.error_message = str(exc)
            logger.warning("query failed parse_error: %s", exc)
            if persist:
                self._safe_record_error(rid, goal_text, resp)
        finally:
            resp.elapsed_ms = (time.perf_counter() - started) * 1000
        return resp

    def compile_only(self, source: str, *, request_id: str | None = None) -> ServiceResponse:
        rid = request_id or new_request_id()
        started = time.perf_counter()
        logger = logging.LoggerAdapter(log, {"request_id": rid})
        resp = ServiceResponse(request_id=rid, status="error")
        try:
            self._compile_eval(source, resp, rid, logger, persist=False)
            resp.status = "ok"
            logger.info("compile_only ok version=%s", resp.version)
        except DatalogError as exc:
            resp.error_category = exc.category
            resp.error_message = str(exc)
            logger.warning("compile_only failed category=%s: %s", exc.category, exc)
        except (ParseError, ValueError) as exc:
            resp.error_category = "parse_error"
            resp.error_message = str(exc)
        finally:
            resp.elapsed_ms = (time.perf_counter() - started) * 1000
        return resp

    def materialize(self, source: str, *, request_id: str | None = None) -> ServiceResponse:
        """Evaluate and persist the full closure; no query goal."""
        rid = request_id or new_request_id()
        started = time.perf_counter()
        logger = logging.LoggerAdapter(log, {"request_id": rid})
        resp = ServiceResponse(request_id=rid, status="error")
        try:
            compiled, result = self._compile_eval(source, resp, rid, logger, persist=True)
            resp.status = "ok"
            total = sum(len(r) for r in result.db.rels.values())
            resp.uncertainty_notes = [f"materialized {total} tuples across {len(result.db.rels)} relations"]
            self.store.record_request(
                request_id=rid,
                kind="materialize",
                status="ok",
                program_id=self._versioned_program_id(compiled.version),
                version=compiled.version,
                answer_count=total,
            )
        except DatalogError as exc:
            resp.error_category, resp.error_message = exc.category, str(exc)
            self._safe_record_error(rid, None, resp)
        except (ParseError, ValueError) as exc:
            resp.error_category, resp.error_message = "parse_error", str(exc)
            self._safe_record_error(rid, None, resp)
        finally:
            resp.elapsed_ms = (time.perf_counter() - started) * 1000
        return resp

    # ------------------------------------------------------------------

    def _compile_eval(
        self,
        source: str,
        resp: ServiceResponse,
        rid: str,
        logger: logging.LoggerAdapter,
        *,
        persist: bool,
    ) -> tuple[CompiledProgram, FixpointResult]:
        program = Parser(source).parse_program()
        compiled = compile_program(program)
        rule_texts = [r.render() for r in program.rules]
        resp.version = compiled.version
        resp.rule_texts = rule_texts
        logger.info("compiled version=%s rules=%d facts=%d", compiled.version, len(rule_texts), len(program.facts))

        if persist:
            self.store.save_program(
                program_id=self._versioned_program_id(compiled.version),
                version=compiled.version,
                source=source,
                rule_texts=rule_texts,
            )

        result = evaluate(compiled)

        resp.strata = [
            {
                "level": t.level,
                "predicates": [f"{p[0]}/{p[1]}" for p in t.predicates],
                "iterations": t.iterations,
                "produced": t.produced,
            }
            for t in result.traces
        ]
        steps: list[str] = []
        for t in result.traces:
            for s in t.steps:
                steps.append(f"[stratum {t.level} | {', '.join(p[0] for p in t.predicates) or 'edb'}] {s}")
        resp.steps = steps

        if persist:
            n = self.store.persist_fixpoint(
                rid, self._versioned_program_id(compiled.version), compiled.version, result
            )
            logger.info("persisted %d tuples", n)
            steps.append(f"[store] persisted {n} tuples for request {rid} (version {compiled.version})")
        return compiled, result

    def _safe_record_error(
        self, rid: str, goal_text: str | None, resp: ServiceResponse, kind: str = "query"
    ) -> None:
        try:
            self.store.record_request(
                request_id=rid,
                kind=kind,
                status="error",
                program_id=None,
                goal=(goal_text or "").strip() or None,
                message=f"{resp.error_category}: {resp.error_message}",
            )
        except Exception:  # pragma: no cover - storage must not mask the real error
            log.exception("failed to record error request %s", rid)

    def _parse_goal(self, goal_text: str, compiled: CompiledProgram) -> Atom:
        parser = Parser(goal_text.strip())
        goal = parser._parse_atom()
        if parser.cur.kind != "EOF":
            from .language.errors import QueryError

            raise QueryError(f"trailing tokens after goal {goal.render()}")
        key = (goal.pred, goal.arity)
        if goal.pred not in compiled.arities:
            raise UnknownPredicateError(
                f"goal predicate {goal.pred}/{goal.arity} does not occur in the program"
            )
        if compiled.arities[goal.pred] != goal.arity:
            raise UnknownPredicateError(
                f"goal {goal.render()} has arity {goal.arity} but the program defines "
                f"{goal.pred}/{compiled.arities[goal.pred]}"
            )
        return goal

    def _answer(
        self, goal: Atom, compiled: CompiledProgram, result: FixpointResult
    ) -> tuple[list[QueryAnswer], list[str]]:
        key = (goal.pred, goal.arity)
        relation = sorted(result.db.rels.get(key, set()))
        rule_texts = dict(enumerate(r.render() for r in compiled.program.rules))
        builder = ProofBuilder(result, rule_texts, compiled.stratum_of)

        answers: list[QueryAnswer] = []
        notes: list[str] = []
        for tup in relation:
            bindings: dict[str, str] = {}
            ok = True
            for term, value in zip(goal.args, tup):
                if isinstance(term, Const):
                    if term.value != value:
                        ok = False
                        break
                else:
                    bindings[term.name] = value
            if not ok:
                continue
            proof = builder.build(key, tup).to_dict()
            uncertain = _tree_has_kind(proof, "uncertain")
            if uncertain:
                notes.append(f"proof for {goal.pred}{list(tup)} contains an uncertain node")
            answers.append(QueryAnswer(bindings=bindings, proof=proof, uncertain=uncertain))
        return answers, notes


def _tree_has_kind(node: dict, kind: str) -> bool:
    if node.get("kind") == kind:
        return True
    return any(_tree_has_kind(c, kind) for c in node.get("children", []))
