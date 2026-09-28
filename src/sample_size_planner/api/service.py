"""Application orchestration: contracts -> solvers -> evidence -> persistence.

This is the only layer that knows about *both* the statistical kernels and the
storage/logging side effects. Keeping it thin makes the kernels independently
testable while the API stays a transport shell.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

import numpy as np

from ..config import Settings, settings as default_settings
from ..contracts import (
    BinomialSpec,
    EstimandFamily,
    FailureCategory,
    NormalSpec,
    TestDirection,
)
from ..diagnostics import interim as interim_mod
from ..estimation.solvers import SolverConfig, solve_binomial, solve_normal
from ..evidence.repository import RunRepository
from ..evidence.run_log import RunIdentity, RunLogger
from ..evidence.simulation import (
    simulate_binomial_power,
    simulate_normal_power,
)
from .schemas import (
    BinomialPlanRequest,
    InterimPlanRequest,
    NormalPlanRequest,
    SimulationRequest,
)


class PlanningService:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or default_settings
        self.settings.ensure_dirs()
        self.repo = RunRepository(self.settings.db_path)

    # ------------------------------------------------------------------ #
    def _new_run(self, logger: Optional[RunLogger], purpose: str) -> tuple[RunIdentity, RunLogger]:
        if logger is not None:
            # Reuse the caller's run identity so all sub-computations correlate
            # to one enclosing run (e.g. one validation script execution).
            run = logger.run
            log = logger
            self.repo.start_run(run.run_id, run.created_at, purpose, run.versions)
            return run, log
        run = RunIdentity.new(purpose)
        log_path = self.settings.runs_dir / f"{run.run_id}.jsonl"
        log = RunLogger(run, log_path)
        self.repo.start_run(run.run_id, run.created_at, purpose, run.versions)
        log.event("run_started", purpose=purpose, versions=run.versions)
        return run, log

    @staticmethod
    def _result_to_dict(result) -> Dict[str, Any]:
        d = asdict(result)
        d["method"] = result.method.value if result.method else None
        d["failure_category"] = result.failure_category.value
        return d

    # ------------------------------------------------------------------ #
    def plan_normal(self, req: NormalPlanRequest,
                    logger: Optional[RunLogger] = None) -> Dict[str, Any]:
        run, log = self._new_run(logger, "plan-normal")
        payload = req.model_dump()
        log.input_recorded("normal_plan_request", payload)
        try:
            spec = NormalSpec(
                alpha=req.alpha, power=req.power, direction=req.direction,
                allocation_ratio=req.allocation_ratio, one_sample=req.one_sample,
                standardized_effect=req.standardized_effect, delta=req.delta,
                sd0=req.sd0, sd1=req.sd1, use_t_distribution=req.use_t_distribution,
            )
            cfg = SolverConfig(max_sample_size=self.settings.max_sample_size,
                               low_rate_threshold=self.settings.low_rate_threshold)
            result = solve_normal(spec, cfg)
        except ValueError as exc:
            log.judgement("failure", "invalid normal spec", error=str(exc))
            return self._fail(run, EstimandFamily.NORMAL, FailureCategory.INVALID_INPUT, str(exc))

        rd = self._result_to_dict(result)
        self.repo.save_plan(run.run_id, "normal", payload, rd)
        verdict = "accept" if result.ok else "failure"
        log.judgement(verdict,
                      "boundary n passes and n-1 fails" if result.ok else result.failure_detail or "",
                      n=result.n_per_group0, n1=result.n_per_group1,
                      power=result.achieved_power, power_n_minus_one=result.power_at_n_minus_one,
                      failure_category=result.failure_category.value, method=rd["method"],
                      trace=result.diagnostics.get("integer_search_trace", []))
        return {"run_id": run.run_id, **rd}

    def plan_binomial(self, req: BinomialPlanRequest,
                      logger: Optional[RunLogger] = None) -> Dict[str, Any]:
        run, log = self._new_run(logger, "plan-binomial")
        payload = req.model_dump()
        log.input_recorded("binomial_plan_request", payload)
        try:
            spec = BinomialSpec(
                p0=req.p0, p1=req.p1, alpha=req.alpha, power=req.power,
                direction=req.direction, allocation_ratio=req.allocation_ratio,
                one_sample=req.one_sample, scale=req.scale,
                continuity_correction=False,
            )
            cfg = SolverConfig(max_sample_size=self.settings.max_sample_size,
                               low_rate_threshold=self.settings.low_rate_threshold)
            result = solve_binomial(spec, cfg, force_exact=req.force_exact)
        except ValueError as exc:
            log.judgement("failure", "invalid binomial spec", error=str(exc))
            return self._fail(run, EstimandFamily.BINOMIAL, FailureCategory.INVALID_INPUT, str(exc))

        rd = self._result_to_dict(result)
        self.repo.save_plan(run.run_id, "binomial", payload, rd)
        verdict = "accept" if result.ok else "failure"
        log.judgement(verdict,
                      "boundary n passes and n-1 fails" if result.ok else result.failure_detail or "",
                      n=result.n_per_group0, n1=result.n_per_group1,
                      power=result.achieved_power, power_n_minus_one=result.power_at_n_minus_one,
                      failure_category=result.failure_category.value, method=rd["method"],
                      low_base_rate=result.diagnostics.get("low_base_rate"),
                      trace=result.diagnostics.get("integer_search_trace", []))
        return {"run_id": run.run_id, **rd}

    # ------------------------------------------------------------------ #
    def run_simulation(self, req: SimulationRequest,
                       logger: Optional[RunLogger] = None) -> Dict[str, Any]:
        run, log = self._new_run(logger, f"simulate-{req.family}")
        payload = req.model_dump()
        seed = req.seed if req.seed is not None else self.settings.sim_seed
        log.input_recorded("simulation_request", {**payload, "resolved_seed": seed})
        rng = np.random.default_rng(seed)
        two_sided = req.direction.is_two_sided
        greater = req.direction is TestDirection.GREATER

        log.progress("drawing synthetic replications", replications=req.replications)
        if req.family == "normal":
            if req.standardized_effect is None:
                raise ValueError("normal simulation needs standardized_effect")
            effect = abs(req.standardized_effect) * req.sd0
            sd1 = req.sd0 if req.sd1 is None else req.sd1
            n1 = None if req.n_per_group1 is None else req.n_per_group1
            sim = simulate_normal_power(
                n0=req.n_per_group0, n1=n1, effect=effect, sd0=req.sd0, sd1=sd1,
                alpha=req.alpha, two_sided=two_sided, use_t=req.use_t_distribution,
                replications=req.replications, rng=rng)
        else:
            if req.p0 is None or req.p1 is None:
                raise ValueError("binomial simulation needs p0 and p1")
            n1 = None if req.n_per_group1 is None else req.n_per_group1
            sim = simulate_binomial_power(
                n0=req.n_per_group0, n1=n1, p0=req.p0, p1=req.p1, alpha=req.alpha,
                two_sided=two_sided, greater=greater, exact_one_sample=req.exact_one_sample,
                replications=req.replications, rng=rng)

        agrees: Optional[bool] = None
        if req.target_power is not None:
            # agreement: the target must lie inside the Monte Carlo 95% CI,
            # and the point estimate must be within the configured tolerance.
            agrees = (sim.ci95_low <= req.target_power <= sim.ci95_high) and (
                abs(sim.estimated_power - req.target_power) <= self.settings.sim_tolerance)
        self.repo.save_simulation(run.run_id, req.family, payload, sim.replications,
                                  sim.estimated_power, sim.ci95_low, sim.ci95_high,
                                  agrees if agrees is not None else False)
        log.judgement("accept" if agrees in (True, None) else "reject",
                      "Monte Carlo CI cross-check",
                      estimated=sim.estimated_power, se=sim.standard_error,
                      ci95=[sim.ci95_low, sim.ci95_high], target=req.target_power,
                      agrees=agrees, replications=sim.replications)
        return {
            "run_id": run.run_id, "replications": sim.replications,
            "estimated_power": sim.estimated_power, "standard_error": sim.standard_error,
            "ci95": [sim.ci95_low, sim.ci95_high], "target_power": req.target_power,
            "agrees_with_target": agrees, "seed": seed,
        }

    # ------------------------------------------------------------------ #
    def plan_interim(self, req: InterimPlanRequest,
                     logger: Optional[RunLogger] = None) -> Dict[str, Any]:
        run, log = self._new_run(logger, "plan-interim")
        payload = req.model_dump()
        log.input_recorded("interim_plan_request", payload)
        try:
            family = interim_mod.BoundaryFamily(req.family)
            plan = interim_mod.plan_interim(
                req.information_times, alpha=req.alpha,
                drift_at_full_information=req.drift_at_full_information,
                two_sided=req.direction.is_two_sided, family=family,
                max_looks=min(req.max_looks, self.settings.max_interim_looks))
        except interim_mod.InterimError as exc:
            log.judgement("failure", "interim commitment violation",
                          failure_category=exc.category.value, error=str(exc))
            return {"run_id": run.run_id, "success": False,
                    "failure_category": exc.category.value, "detail": str(exc)}

        schedule = {
            "information_times": list(plan.schedule.information_times),
            "z_boundaries": list(plan.schedule.z_boundaries),
            "cumulative_alpha_spent": list(plan.cumulative_alpha_spent),
        }
        self.repo.save_interim(run.run_id, req.family, payload, schedule,
                               plan.cumulative_alpha_spent[-1], plan.alternative_power)
        log.judgement("accept", "committed group-sequential schedule",
                      final_size=plan.cumulative_alpha_spent[-1],
                      alt_power=plan.alternative_power,
                      boundaries=list(plan.schedule.z_boundaries))
        return {
            "run_id": run.run_id, "family": req.family,
            "information_times": list(plan.schedule.information_times),
            "z_boundaries": list(plan.schedule.z_boundaries),
            "cumulative_alpha_spent": list(plan.cumulative_alpha_spent),
            "per_look_nominal_alpha": list(plan.per_look_nominal_alpha),
            "alternative_power": plan.alternative_power,
            "fixed_sample_power": plan.fixed_sample_power,
            "power_loss_vs_fixed": plan.power_loss_vs_fixed,
        }

    # ------------------------------------------------------------------ #
    def _fail(self, run: RunIdentity, family: EstimandFamily,
              category: FailureCategory, detail: str) -> Dict[str, Any]:
        return {
            "run_id": run.run_id, "success": False, "family": family.value,
            "failure_category": category.value, "failure_detail": detail,
            "n_per_group0": None, "n_per_group1": None, "n_total": None,
            "achieved_power": None, "method": None,
            "power_at_n_minus_one": None, "diagnostics": {},
        }
