"""Service layer: orchestrates solver, independent replay and evidence."""
from __future__ import annotations

import logging

from app.planner.enumerate import exhaustive_reference
from app.planner.replay import replay
from app.planner.solver import SolverConfig, solve
from app.rules.models import Plan, Problem, ReplayResult, SearchResult
from app.storage import EvidenceStore

logger = logging.getLogger("temporal-planner.service")


class SolveOutcome:
    def __init__(
        self,
        *,
        run_id: str,
        fingerprint: str,
        engine_version: str,
        search: SearchResult,
        replay_result: ReplayResult | None,
        reference: object | None,
        cross_check_agrees: bool | None,
        cross_check_detail: str | None,
    ) -> None:
        self.run_id = run_id
        self.fingerprint = fingerprint
        self.engine_version = engine_version
        self.search = search
        self.replay_result = replay_result
        self.reference = reference
        self.cross_check_agrees = cross_check_agrees
        self.cross_check_detail = cross_check_detail


class PlanningService:
    def __init__(self, store: EvidenceStore, engine_version: str) -> None:
        self.store = store
        self.engine_version = engine_version

    def solve(
        self,
        problem: Problem,
        *,
        budget_nodes: int,
        max_steps: int,
        max_occurrences_per_action: int | None,
        cross_check: bool,
        reference_max_steps: int,
        reference_max_occurrences: int,
    ) -> SolveOutcome:
        config = SolverConfig(
            budget_nodes=budget_nodes,
            max_steps=max_steps,
            max_occurrences_per_action=max_occurrences_per_action,
        )
        search = solve(problem, config)
        run_id = self.store.save_search(problem, search)
        logger.info(
            "solve run_id=%s problem=%r status=%s optimal=%s makespan=%s nodes=%s/%s",
            run_id,
            problem.name,
            search.status.value,
            search.optimal,
            search.makespan,
            search.nodes_expanded,
            search.budget_nodes,
        )

        replay_result: ReplayResult | None = None
        if search.plan is not None:
            replay_result = replay(problem, search.plan)
            self.store.save_replay(problem, search.plan, replay_result, parent_run_id=run_id)
            if not replay_result.is_valid:
                # The solver gates on replay itself; reaching here is an
                # internal inconsistency that must never look like success.
                logger.error("run_id=%s produced a plan that failed independent replay", run_id)

        reference = None
        agrees: bool | None = None
        detail: str | None = None
        if cross_check:
            reference = exhaustive_reference(
                problem,
                max_steps=reference_max_steps,
                max_occurrences_per_action=reference_max_occurrences,
            )
            solver_found = search.plan is not None and replay_result is not None and replay_result.is_valid
            agrees = reference.found == solver_found and (
                reference.optimal_makespan == search.makespan if reference.found and solver_found else True
            )
            detail = (
                f"reference evaluated {reference.schedules_evaluated} schedules; "
                f"reference_found={reference.found}, solver_found={solver_found}"
            )
            self.store.save_reference_check(
                run_id,
                reference_kind="small_grid_exhaustive",
                reference_found=reference.found,
                reference_makespan=reference.optimal_makespan,
                solver_found=solver_found,
                solver_makespan=search.makespan,
                agree=bool(agrees),
                schedules_evaluated=reference.schedules_evaluated,
                detail=detail,
            )
            logger.info(
                "run_id=%s reference cross-check agree=%s reference_found=%s reference_makespan=%s",
                run_id,
                agrees,
                reference.found,
                reference.optimal_makespan,
            )

        record = self.store.get_run(run_id)
        return SolveOutcome(
            run_id=run_id,
            fingerprint=record["input_fingerprint"],
            engine_version=record["engine_version"],
            search=search,
            replay_result=replay_result,
            reference=reference,
            cross_check_agrees=agrees,
            cross_check_detail=detail,
        )

    def replay_only(self, problem: Problem, plan: Plan):
        result = replay(problem, plan)
        run_id = self.store.save_replay(problem, plan, result)
        logger.info(
            "replay run_id=%s problem=%r outcome=%s violations=%s goal_satisfied=%s",
            run_id,
            problem.name,
            result.outcome.value,
            len(result.violations),
            result.goal_satisfied,
        )
        record = self.store.get_run(run_id)
        return run_id, record["input_fingerprint"], record["engine_version"], result

    def reference_only(
        self, problem: Problem, *, max_steps: int, max_occurrences_per_action: int
    ):
        return exhaustive_reference(
            problem,
            max_steps=max_steps,
            max_occurrences_per_action=max_occurrences_per_action,
        )
