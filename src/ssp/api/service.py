"""Service layer: map web payloads to core specs and run reproducible experiments."""
from __future__ import annotations

from typing import Any

from ssp.contracts import (
    Alternative,
    BinomialEffectScale,
    BinomialSpec,
    MethodPreference,
    NormalEffectScale,
    NormalSpec,
)
from ssp.diagnostics import RunLogger, input_fingerprint, new_run_id, numerical_versions
from ssp.errors import PlannerError, ValidationError
from ssp.repro import ExperimentRecord, ExperimentRunner
from ssp.storage import RunStore

from .schemas import BinomialPlanRequest, NormalPlanRequest


def _enum(cls, value: str):
    try:
        return cls(value)
    except ValueError as exc:
        raise ValidationError(f"invalid enum value {value!r}", details={"value": value}) from exc


def build_normal_spec(req: NormalPlanRequest) -> NormalSpec:
    spec = NormalSpec(
        alternative=_enum(Alternative, req.alternative),
        alpha=req.alpha,
        target_power=req.target_power,
        effect=req.effect,
        effect_scale=_enum(NormalEffectScale, req.effect_scale),
        two_sample=req.two_sample,
        sigma=req.sigma,
        known_sigma=req.known_sigma,
        allocation_ratio=req.allocation_ratio,
        interim_looks=req.interim_looks,
        method_preference=_enum(MethodPreference, req.method_preference),
    )
    return spec.validate()


def build_binomial_spec(req: BinomialPlanRequest) -> BinomialSpec:
    spec = BinomialSpec(
        alternative=_enum(Alternative, req.alternative),
        alpha=req.alpha,
        target_power=req.target_power,
        p0=req.p0,
        effect=req.effect,
        effect_scale=_enum(BinomialEffectScale, req.effect_scale),
        two_sample=req.two_sample,
        allocation_ratio=req.allocation_ratio,
        interim_looks=req.interim_looks,
        method_preference=_enum(MethodPreference, req.method_preference),
    )
    return spec.validate()


class PlanningService:
    """Application service: identity, logging, experiment, persistence."""

    def __init__(self, store: RunStore | None = None, runner: ExperimentRunner | None = None):
        self.store = store or RunStore()
        self.runner = runner or ExperimentRunner()

    def _open_log(self, spec_dict: dict[str, Any]) -> tuple[str, str, RunLogger]:
        run_id = new_run_id()
        fp = input_fingerprint(spec_dict)
        logger = RunLogger(run_id, fp)
        logger.info(
            "run_start",
            versions=numerical_versions(),
            request=spec_dict,
        )
        return run_id, fp, logger

    def plan_normal(self, req: NormalPlanRequest) -> dict[str, Any]:
        spec_dict = req.model_dump()
        run_id, fp, logger = self._open_log(spec_dict)
        try:
            spec = build_normal_spec(req)
            record = self.runner.run_normal(
                spec, run_id, fp, logger, trials=req.mc_trials
            )
            self.store.save_success(record.to_dict(), spec_dict)
            return self._flatten(record)
        except PlannerError as exc:
            self._persist_failure("normal", run_id, fp, spec_dict, exc, logger)
            raise

    def plan_binomial(self, req: BinomialPlanRequest) -> dict[str, Any]:
        spec_dict = req.model_dump()
        run_id, fp, logger = self._open_log(spec_dict)
        try:
            spec = build_binomial_spec(req)
            record = self.runner.run_binomial(
                spec, run_id, fp, logger, trials=req.mc_trials
            )
            self.store.save_success(record.to_dict(), spec_dict)
            return self._flatten(record)
        except PlannerError as exc:
            self._persist_failure("binomial", run_id, fp, spec_dict, exc, logger)
            raise

    @staticmethod
    def _flatten(record: ExperimentRecord) -> dict[str, Any]:
        """Plan verdict at top level with the independent evidence attached."""
        body = record.plan.to_dict()
        body["evidence"] = record.evidence.to_dict()
        body["agreement"] = record.agreement
        return body

    def _persist_failure(
        self, endpoint: str, run_id: str, fp: str, spec_dict: dict[str, Any],
        exc: PlannerError, logger: RunLogger,
    ) -> None:
        logger.error("run_failed", error_category=exc.category.value, message=str(exc), details=exc.details)
        try:
            self.store.save_failure(run_id, endpoint, fp, spec_dict, exc, numerical_versions())
        except PlannerError:
            logger.error("persistence_failed_after_planning_error", original_error=exc.category.value)
