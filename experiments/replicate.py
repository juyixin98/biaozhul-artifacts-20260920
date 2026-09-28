"""Monte-Carlo replication experiments.

Run:
    PYTHONPATH=src:. python experiments/replicate.py --reps 400
    PYTHONPATH=src:. python experiments/replicate.py --quick

Produces:
    reports/replication.json     machine-readable results
    reports/replication.md       human-readable summary

Experiments
-----------
E1 consistency      strong IV: 2SLS converges to beta0 while OLS stays biased;
                    weak IV: 2SLS is pulled toward the biased OLS limit.
E2 SE coverage      95% CI coverage of beta under homoskedastic and
                    heteroskedastic errors, for both covariance estimators.
E3 Sargan calibration / power: rejection rates with valid and invalid IVs.
E4 endogeneity test power with known endogeneity and size under exogeneity.
E5 failure categories: the estimator returns the correct status/verdict on
                    rank-failure / duplicate / weak / collinear fixtures.

All truth values come from the DGP configs, never from the estimator itself.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from experiments.dgp import (
    DGPConfig,
    generate,
)
from twosls.contract import EstimationOptions, EstimationRequest, InstrumentValidityClaim, ModelSpec
from twosls.errors import UnidentifiedError, WeakInstrumentError
from twosls.estimator import estimate

logging.disable(logging.CRITICAL)
ROOT = Path(__file__).resolve().parents[1]

BETA0 = 0.75


def _single_endog_request(columns: dict, request_id: str, covariance: str = "homoskedastic",
                          strict: bool = False) -> EstimationRequest:
    n = len(columns["y"])
    names_w = sorted(k for k in columns if k.startswith("w"))
    spec = ModelSpec(
        dependent="y",
        endogenous=["x_end"],
        included_exogenous=[*names_w, "const"],
        excluded_instruments=sorted(k for k in columns if k.startswith("z")),
    )
    cols = {k: np.asarray(v, dtype=float).tolist() for k, v in columns.items()}
    cols.setdefault("const", [1.0] * n)
    return EstimationRequest(
        request_id=request_id,
        columns=cols,
        spec=spec,
        options=EstimationOptions(covariance=covariance, strict=strict),
        validity_claim=InstrumentValidityClaim(exclusion_restriction_asserted=True),
    )


@dataclass
class MCResult:
    name: str
    reps: int
    metrics: dict


def experiment_consistency(reps: int) -> list[MCResult]:
    rows = []
    scenarios = {
        "strong_iv_n1000": DGPConfig(nobs=1000, pi=(0.6, 0.6), endogeneity=0.6, seed=0),
        "strong_iv_n4000": DGPConfig(nobs=4000, pi=(0.6, 0.6), endogeneity=0.6, seed=0),
        "weak_iv_n4000": DGPConfig(nobs=4000, pi=(0.04, 0.04), endogeneity=0.6, seed=0),
    }
    for label, cfg in scenarios.items():
        iv_b, ols_b, weak_count = [], [], 0
        for r in range(reps):
            sample = generate(DGPConfig(**{**asdict(cfg), "seed": 10_000 + r}))
            req = _single_endog_request(sample.columns, f"mc-{label}-{r}")
            out = estimate(req)
            iv_b.append(out.coefficient_map()["x_end"].estimate)
            ols_b.append(out.endogeneity.ols_contrast[0])
            weak_count += out.status == "weak"
        rows.append(MCResult(label, reps, {
            "beta0": BETA0,
            "iv_mean": float(np.mean(iv_b)),
            "iv_bias": float(np.mean(iv_b) - BETA0),
            "iv_rmse": float(np.sqrt(np.mean((np.array(iv_b) - BETA0) ** 2))),
            "ols_mean": float(np.mean(ols_b)),
            "ols_bias": float(np.mean(ols_b) - BETA0),
            "fraction_flagged_weak": weak_count / reps,
        }))
    return rows


def experiment_coverage(reps: int) -> list[MCResult]:
    """95% CI coverage. Heteroskedastic errors via scale depending on |Z1|."""
    rows = []
    for error_kind, hetero in [("homoskedastic", False), ("heteroskedastic", True)]:
        for cov in ["homoskedastic", "robust"]:
            covers, ses, betas = [], [], []
            for r in range(reps):
                rng = np.random.default_rng(20_000 + r)
                n = 1000
                Z = rng.standard_normal((n, 2))
                Wx = rng.standard_normal(n)
                v = rng.standard_normal(n)
                base_e = 0.6 * v + rng.standard_normal(n)
                scale = (0.5 + np.abs(Z[:, 0])) if hetero else np.ones(n)
                e = base_e * scale
                x_end = Z @ np.array([0.6, 0.6]) + 0.4 * Wx + v
                y = BETA0 * x_end + 0.4 * Wx + e
                cols = {"y": y, "x_end": x_end, "z1": Z[:, 0], "z2": Z[:, 1], "w1": Wx}
                out = estimate(_single_endog_request(cols, f"cov-{error_kind}-{r}", covariance=cov))
                c = out.coefficient_map()["x_end"]
                covers.append(c.ci_low <= BETA0 <= c.ci_high)
                ses.append(c.std_error)
                betas.append(c.estimate)
            rows.append(MCResult(f"coverage_{error_kind}_{cov}", reps, {
                "coverage95": float(np.mean(covers)),
                "mean_se": float(np.mean(ses)),
                "empirical_sd_of_estimate": float(np.std(betas, ddof=1)),
            }))
    return rows


def experiment_sargan(reps: int) -> list[MCResult]:
    rows = []
    # size: valid instruments
    rejections = []
    for r in range(reps):
        sample = generate(DGPConfig(nobs=2000, pi=(0.6, 0.6), endogeneity=0.6, seed=30_000 + r))
        out = estimate(_single_endog_request(sample.columns, f"sargan-size-{r}"))
        rejections.append(out.overidentification.p_value < 0.05)
    rows.append(MCResult("sargan_size", reps, {"rejection_rate_5pct": float(np.mean(rejections))}))

    # power: contaminated instrument
    rejections = []
    for r in range(reps):
        sample = generate(DGPConfig(
            nobs=4000, pi=(0.6, 0.6), endogeneity=0.6,
            invalid_instrument_corr=1.2, seed=40_000 + r))
        out = estimate(_single_endog_request(sample.columns, f"sargan-power-{r}"))
        rejections.append(out.overidentification.p_value < 0.05)
    rows.append(MCResult("sargan_power", reps, {"rejection_rate_5pct": float(np.mean(rejections))}))
    return rows


def experiment_endogeneity(reps: int) -> list[MCResult]:
    rows = []
    for label, cov_ve in [("power_endogenous", 0.6), ("size_exogenous", 0.0)]:
        rejections = []
        for r in range(reps):
            sample = generate(DGPConfig(nobs=2000, pi=(0.6, 0.6), endogeneity=cov_ve,
                                        seed=50_000 + r))
            out = estimate(_single_endog_request(sample.columns, f"dwh-{label}-{r}"))
            rejections.append(out.endogeneity.p_value < 0.05)
        rows.append(MCResult(f"dwh_{label}", reps, {"rejection_rate_5pct": float(np.mean(rejections))}))
    return rows


def experiment_failure_categories() -> list[MCResult]:
    """One-shot categorical checks; reference outcomes are asserted here."""
    from experiments.dgp import (
        collinear_iv_sample,
        duplicate_instrument_sample,
        rank_failure_sample,
        underidentified_sample,
        weak_iv_sample,
    )

    checks: list[MCResult] = []

    def category(factory, rid) -> str:
        sample = factory()
        try:
            out = estimate(_single_endog_request(sample.columns, rid))
            return out.status
        except UnidentifiedError:
            return "unidentified"
        except WeakInstrumentError:
            return "weak_strict"

    expected = {
        "rank_failure": (rank_failure_sample, "unidentified"),
        "duplicate_instrument": (duplicate_instrument_sample, "unidentified"),
        "population_zero_pi": (underidentified_sample, "weak"),
        "weak": (weak_iv_sample, "weak"),
        "collinear": (collinear_iv_sample, "ok"),
    }
    for name, (factory, want) in expected.items():
        got = category(factory, f"cat-{name}")
        checks.append(MCResult(f"category_{name}", 1,
                               {"expected": want, "observed": got, "correct": got == want}))
    return checks


def run_all(reps: int) -> dict:
    return {
        "config": {"reps": reps, "beta0": BETA0, "nominal_coverage": 0.95},
        "e1_consistency": [asdict(r) for r in experiment_consistency(reps)],
        "e2_coverage": [asdict(r) for r in experiment_coverage(reps)],
        "e3_sargan": [asdict(r) for r in experiment_sargan(reps)],
        "e4_endogeneity": [asdict(r) for r in experiment_endogeneity(reps)],
        "e5_failure_categories": [asdict(r) for r in experiment_failure_categories()],
    }


def render_markdown(report: dict) -> str:
    lines = ["# 2SLS replication report", "",
             f"reps per experiment: {report['config']['reps']}, truth beta = {report['config']['beta0']}", ""]

    lines.append("## E1 Consistency (IV vs OLS)")
    lines.append("| scenario | IV mean | IV bias | IV RMSE | OLS mean | OLS bias | flagged weak |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in report["e1_consistency"]:
        m = r["metrics"]
        lines.append(f"| {r['name']} | {m['iv_mean']:.3f} | {m['iv_bias']:+.3f} | {m['iv_rmse']:.3f} "
                     f"| {m['ols_mean']:.3f} | {m['ols_bias']:+.3f} | {m['fraction_flagged_weak']:.2f} |")

    lines += ["", "## E2 95% CI coverage", "| setting | coverage | mean SE |", "|---|---|---|"]
    for r in report["e2_coverage"]:
        lines.append(f"| {r['name']} | {r['metrics']['coverage95']:.3f} | {r['metrics']['mean_se']:.4f} |")

    lines += ["", "## E3 Sargan size and power", "| experiment | rejection @5% |", "|---|---|"]
    for r in report["e3_sargan"]:
        lines.append(f"| {r['name']} | {r['metrics']['rejection_rate_5pct']:.3f} |")

    lines += ["", "## E4 Endogeneity test", "| experiment | rejection @5% |", "|---|---|"]
    for r in report["e4_endogeneity"]:
        lines.append(f"| {r['name']} | {r['metrics']['rejection_rate_5pct']:.3f} |")

    lines += ["", "## E5 Failure categories", "| fixture | expected | observed | ok |", "|---|---|---|---|"]
    for r in report["e5_failure_categories"]:
        m = r["metrics"]
        lines.append(f"| {r['name']} | {m['expected']} | {m['observed']} | {'YES' if m['correct'] else 'NO'} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="2SLS Monte-Carlo replications")
    parser.add_argument("--reps", type=int, default=400)
    parser.add_argument("--quick", action="store_true", help="80 reps for a fast smoke run")
    args = parser.parse_args()
    reps = 80 if args.quick else args.reps

    report = run_all(reps)
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "replication.json").write_text(json.dumps(report, indent=2))
    (out_dir / "replication.md").write_text(render_markdown(report))
    print(render_markdown(report))
    print(f"\nwrote {out_dir/'replication.json'} and {out_dir/'replication.md'}")


if __name__ == "__main__":
    main()
