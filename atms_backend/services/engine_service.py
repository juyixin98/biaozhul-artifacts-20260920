"""ATMS service: the real workflow boundary between API, kernel and storage."""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from ..core.atms import ATMS
from ..core.budgets import Budgets
from ..diagnostics import (
    ACCEPTED,
    REJECTED,
    UNDETERMINED,
    REASON_INCOMPLETE,
    REASON_NOGOOD,
    REASON_NO_ENV,
    REASON_SUPPORTED,
    DecisionRecord,
    new_request_id,
)
from ..rules.compiler import compile_ruleset
from ..rules.language import RuleSet, parse_rules
from ..storage.repository import Repository


def env_json(env: FrozenSet[str]) -> List[str]:
    return sorted(env)


def nogoods_json(engine: ATMS) -> List[List[str]]:
    """Canonical, deterministic nogood ordering for API payloads."""
    return [env_json(e) for e in
            sorted(engine.nogoods, key=lambda e: (len(e), env_json(e)))]


def label_payload(engine: ATMS) -> Dict[str, List[List[str]]]:
    """All non-empty consistent labels (facts live under the empty env)."""
    out: Dict[str, List[List[str]]] = {}
    for node in sorted(engine.labels):
        envs = engine.holding_environments(node)
        if envs:
            out[node] = [env_json(e) for e in envs]
    return out


class ATMService:
    def __init__(self, repo: Repository, budgets: Optional[Budgets] = None) -> None:
        self.repo = repo
        self.budgets = budgets or Budgets()

    # -------------------------------------------------------------- lifecycle

    def create_problem(self, problem_id: str, name: str, source: str) -> dict:
        rs = parse_rules(source)  # raises RuleLanguageError on bad input
        self.repo.save_problem(problem_id, name, source, rs)
        return {"id": problem_id, "nodes": sorted(rs.all_node_ids())}

    def _build_engine(self, rs: RuleSet) -> ATMS:
        return compile_ruleset(rs, ATMS(self.budgets))

    def _load_ruleset(self, problem_id: str) -> RuleSet:
        return parse_rules(self.repo.get_problem_source(problem_id))

    def propagate(
        self,
        problem_id: str,
        request_id: Optional[str] = None,
        budgets_override: Optional[Budgets] = None,
    ) -> Tuple[ATMS, dict]:
        """(Re)build, propagate, persist snapshot and a run record.

        ``budgets_override`` applies to this run only.
        """
        request_id = request_id or new_request_id()
        rs = self._load_ruleset(problem_id)
        engine = compile_ruleset(rs, ATMS(budgets_override or self.budgets))
        snapshot = self.repo.load_snapshot(problem_id)
        # A tighter/looser override must compute under ITS OWN budgets from
        # scratch: hydrating a complete snapshot could hand it more label
        # environments than the override allows.
        resumed = snapshot is not None and budgets_override is None
        if snapshot is not None and budgets_override is None:
            labels, nogoods = snapshot
            engine.hydrate(labels, nogoods)
        result = engine.propagate()
        if not result.incomplete and budgets_override is None:
            # Only a completed fixpoint is a trustworthy durable snapshot.
            self.repo.replace_snapshot(
                problem_id,
                {n: engine.holding_environments(n) for n in engine.labels},
                list(engine.nogoods),
            )
        run_id = self.repo.record_run(problem_id, request_id, result)
        summary = {
            "request_id": request_id,
            "run_id": run_id,
            "resumed": resumed,
            **result.as_dict(),
            "nogoods": nogoods_json(engine),
            "labels": label_payload(engine),
        }
        return engine, summary

    # ----------------------------------------------------------------- queries

    def current_engine(
        self,
        problem_id: str,
        request_id: Optional[str] = None,
        budgets_override: Optional[Budgets] = None,
    ) -> ATMS:
        """Return an engine at a known fixpoint, propagating/persisting if needed.

        A ``budgets_override`` runs this computation under tighter limits
        without touching the service default; an incomplete result is
        reported but never persisted as truth.
        """
        rs = self._load_ruleset(problem_id)
        engine = compile_ruleset(rs, ATMS(budgets_override or self.budgets))
        snapshot = self.repo.load_snapshot(problem_id)
        if snapshot is not None and budgets_override is None:
            engine.hydrate(snapshot[0], snapshot[1])
        if not _fixpoint_known(engine):
            result = engine.propagate()
            if not result.incomplete and budgets_override is None:
                self.repo.replace_snapshot(
                    problem_id,
                    {n: engine.holding_environments(n) for n in engine.labels},
                    list(engine.nogoods),
                )
            self.repo.record_run(
                problem_id, request_id or new_request_id(), result
            )
        return engine

    def _decision(
        self,
        request_id: str,
        problem_id: str,
        engine: ATMS,
        node_id: str,
        context: Optional[FrozenSet[str]],
    ) -> DecisionRecord:
        supports = engine.holding_environments(node_id)
        blockers: List[FrozenSet[str]] = []
        if context is not None:
            blockers = sorted((ng for ng in engine.nogoods if ng <= context),
                           key=lambda e: (len(e), env_json(e)))

        if blockers:
            decision, reason = REJECTED, REASON_NOGOOD
        elif context is None:
            if supports:
                decision, reason = ACCEPTED, REASON_SUPPORTED
            elif engine.incomplete:
                decision, reason = UNDETERMINED, REASON_INCOMPLETE
            else:
                decision, reason = REJECTED, REASON_NO_ENV
        else:
            in_context = [e for e in supports if e <= context]
            if in_context:
                decision, reason = ACCEPTED, REASON_SUPPORTED
                supports = in_context
            elif engine.incomplete:
                decision, reason = UNDETERMINED, REASON_INCOMPLETE
            else:
                decision, reason = REJECTED, REASON_NO_ENV

        return DecisionRecord(
            request_id=request_id,
            problem_id=problem_id,
            node_id=node_id,
            decision=decision,
            reason=reason,
            query_environment=sorted(context) if context is not None else None,
            supporting_environments=[env_json(e) for e in supports],
            nogood_blockers=[env_json(e) for e in blockers],
            nogoods=nogoods_json(engine),
            incomplete=engine.incomplete,
            incomplete_reason=engine.incomplete_reason,
        )

    def query_node(
        self,
        problem_id: str,
        node_id: str,
        context: Optional[Sequence[str]] = None,
        request_id: Optional[str] = None,
        budgets_override: Optional[Budgets] = None,
    ) -> Tuple[ATMS, DecisionRecord]:
        request_id = request_id or new_request_id()
        rs = self._load_ruleset(problem_id)
        if context is not None:
            unknown = set(context) - set(rs.assumptions)
            if unknown:
                raise ValueError(
                    f"context references undeclared assumptions: {sorted(unknown)}"
                )
        engine = self.current_engine(
            problem_id, request_id, budgets_override=budgets_override
        )
        ctx = frozenset(context) if context is not None else None
        return engine, self._decision(
            request_id, problem_id, engine, node_id, ctx
        )

    def explain_node(self, problem_id: str, node_id: str) -> dict:
        engine = self.current_engine(problem_id)
        explanations = [
            {
                "environment": env_json(env),
                "rules": via,
            }
            for env, via in engine.explain(node_id)
        ]
        return {
            "problem_id": problem_id,
            "node_id": node_id,
            "explanations": explanations,
            "incomplete": engine.incomplete,
        }

    def nogoods(self, problem_id: str) -> dict:
        engine = self.current_engine(problem_id)
        return {
            "problem_id": problem_id,
            "nogoods": nogoods_json(engine),
            "incomplete": engine.incomplete,
        }

    # -------------------------------------------------- retraction (contract 2)

    def retract_assumptions(
        self,
        problem_id: str,
        withdrawn: Sequence[str],
        request_id: Optional[str] = None,
    ) -> dict:
        """Scenario: recompute labels as if assumptions were never declared.

        Rules are kept intact; an undeclared assumption simply has no
        singleton environment, so derivations whose *only* support
        required it disappear while conclusions with alternative
        environments survive.  The stored problem is not modified -- this
        is a hypothetical scenario and is audited as a propagation run.
        """
        request_id = request_id or new_request_id()
        rs = self._load_ruleset(problem_id)
        withdrawn_set = set(withdrawn)
        unknown = withdrawn_set - set(rs.assumptions)
        if unknown:
            raise KeyError(f"not declared assumptions: {sorted(unknown)}")

        before = self._build_engine(rs)
        before_result = before.propagate()
        before_nodes = {
            n: set(before.holding_environments(n))
            for n in before.labels
            if before.holding_environments(n)
        }

        rs_after = RuleSet(
            assumptions=tuple(a for a in rs.assumptions if a not in withdrawn_set),
            facts=rs.facts,
            rules=rs.rules,
        )
        after = self._build_engine(rs_after)
        result = after.propagate()
        self.repo.record_run(problem_id, request_id, result)

        after_nodes = {
            n: set(after.holding_environments(n))
            for n in after.labels
            if after.holding_environments(n)
        }
        surviving = sorted(set(before_nodes) & set(after_nodes))
        removed = sorted(set(before_nodes) - set(after_nodes))
        changed: List[dict] = []
        for node in sorted(set(before_nodes) | set(after_nodes)):
            b = {tuple(env_json(e)) for e in before_nodes.get(node, set())}
            a_envs = {tuple(env_json(e)) for e in after_nodes.get(node, set())}
            if b != a_envs:
                changed.append({
                    "node_id": node,
                    "before": sorted(list(x) for x in b),
                    "after": sorted(list(x) for x in a_envs),
                    "survives": node in after_nodes,
                })
        return {
            "request_id": request_id,
            "problem_id": problem_id,
            "withdrawn": sorted(withdrawn_set),
            "surviving_nodes": surviving,
            "removed_nodes": removed,
            "changed": changed,
            "nogoods": nogoods_json(after),
            "incomplete": result.incomplete or before_result.incomplete,
            "incomplete_reason": result.reason or before_result.reason,
        }


def _fixpoint_known(engine: ATMS) -> bool:
    """Whether the engine already reflects a complete fixpoint snapshot.

    Only complete fixpoints are persisted (partial, budget-stopped states
    are recorded as runs but never served as truth), so a hydrated engine
    is immediately queryable.
    """
    return engine.fixpoint_reached
