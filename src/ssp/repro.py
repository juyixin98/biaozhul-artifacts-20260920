"""Reproducibility layer.

* :func:`derive_seed` derives deterministic RNG seeds from a run identity, so a
  re-run with the same inputs reproduces the same Monte-Carlo evidence.
* :class:`ExperimentRunner` ties a plan together with independent simulated
  power evidence and records the agreement judgement.
* Fixtures are the local synthetic scenarios shipped under ``data/fixtures``;
  no real participant data is involved.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .contracts import BinomialSpec, NormalSpec, PlanResult
from .diagnostics import RunLogger
from .evidence import (
    SimulationEvidence,
    simulate_binomial_power_asymptotic,
    simulate_binomial_power_exact,
    simulate_normal_power,
)
from .errors import SimulationError
from .planning import plan_binomial, plan_normal

FIXTURE_SUBDIR = Path("data") / "fixtures"


def derive_seed(run_id: str, purpose: str) -> int:
    """Deterministic 63-bit seed from run id and purpose."""
    digest = hashlib.sha256(f"{run_id}:{purpose}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


@dataclass(frozen=True)
class ExperimentRecord:
    """Full reproducible record: analytical plan + independent evidence."""

    plan: PlanResult
    evidence: SimulationEvidence
    agreement: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "evidence": self.evidence.to_dict(),
            "agreement": self.agreement,
        }


class ExperimentRunner:
    """Runs the planning kernel and an independent simulation, then judges."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def run_normal(
        self,
        spec: NormalSpec,
        run_id: str,
        fingerprint: str,
        logger: RunLogger,
        trials: int | None = None,
    ) -> ExperimentRecord:
        plan = plan_normal(spec, run_id, fingerprint, logger, self.settings)
        seed = derive_seed(run_id, "normal_power_simulation")
        evidence = simulate_normal_power(
            spec, plan.allocation, trials or self.settings.mc_default_trials, seed, logger
        )
        return ExperimentRecord(plan, evidence, judge_agreement(plan, evidence, logger))

    def run_binomial(
        self,
        spec: BinomialSpec,
        run_id: str,
        fingerprint: str,
        logger: RunLogger,
        trials: int | None = None,
    ) -> ExperimentRecord:
        plan = plan_binomial(spec, run_id, fingerprint, logger, self.settings)
        seed = derive_seed(run_id, f"binomial_power_simulation:{plan.method}")
        if plan.method.startswith("fisher") or plan.method.startswith("binomial_exact"):
            evidence = simulate_binomial_power_exact(
                spec, plan.allocation, trials or self.settings.mc_default_trials, seed, logger
            )
        else:
            evidence = simulate_binomial_power_asymptotic(
                spec, plan.allocation, trials or self.settings.mc_default_trials, seed, logger
            )
        return ExperimentRecord(plan, evidence, judge_agreement(plan, evidence, logger))


def judge_agreement(
    plan: PlanResult, evidence: SimulationEvidence, logger: RunLogger
) -> dict[str, Any]:
    """Compare analytical achieved power with independent simulated power.

    Agreement means the analytic value lies inside the simulation's 95%
    binomial interval.  A mismatch is reported explicitly (never forced to
    agree), together with the standardized residual.
    """
    lo, hi = evidence.ci95_low, evidence.ci95_high
    analytic = plan.achieved_power
    agrees = lo <= analytic <= hi
    residual = (analytic - evidence.estimated_power) / max(evidence.mc_standard_error, 1e-12)
    verdict = {
        "analytic_power": analytic,
        "simulated_power": evidence.estimated_power,
        "simulation_ci95": [lo, hi],
        "standardized_residual": residual,
        "agrees": agrees,
        "basis": "analytic power inside the simulation 95% binomial CI",
    }
    logger.info("agreement_judgement", **verdict)
    if not agrees:
        logger.warning("agreement_mismatch", **verdict)
    return verdict


# ---------------------------------------------------------------------------
# Synthetic local fixtures
# ---------------------------------------------------------------------------


def fixture_path(name: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.project_root / FIXTURE_SUBDIR / name


def load_fixture(name: str, settings: Settings | None = None) -> dict[str, Any]:
    path = fixture_path(name, settings)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SimulationError(
            f"could not load fixture {name}", details={"path": str(path), "error": str(exc)}
        ) from exc


def list_fixtures(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    directory = settings.project_root / FIXTURE_SUBDIR
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.glob("*.json"))
