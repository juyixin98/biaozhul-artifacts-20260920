"""End-to-end demo: synthetic baseline/current windows -> drift report.

Runs every built-in scenario, fits the monitor on the baseline, scores
the current window, and also trains the tiny logistic model so feature
drift can be seen next to a performance proxy. Deterministic (fixed seed).

Usage::

    python scripts/demo.py
    python scripts/demo.py --scenario mean_shift --json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift_monitor import DriftMonitor, make_dataset  # noqa: E402
from drift_monitor.model import LogisticModel, roc_auc  # noqa: E402
from drift_monitor.synthetic import SCENARIOS, FEATURE_NAMES  # noqa: E402


def _run(scenario: str, seed: int = 42) -> dict:
    ds = make_dataset(scenario, seed=seed)
    monitor = DriftMonitor(n_bins=10, strategy="quantile", alpha=0.5)
    monitor.fit(ds.X_base)
    report = monitor.score(ds.X_cur).to_dict()

    model = LogisticModel(FEATURE_NAMES).fit(ds.X_base, ds.y_base)
    auc_base = roc_auc(ds.y_base, model.predict_proba(ds.X_base))
    auc_cur = roc_auc(ds.y_cur, model.predict_proba(ds.X_cur))
    report["scenario"] = scenario
    report["model_auc"] = {
        "baseline": None if math.isnan(auc_base) else round(auc_base, 4),
        "current": None if math.isnan(auc_cur) else round(auc_cur, 4),
    }
    return report


def _print_human(report: dict) -> None:
    print(f"\n=== scenario: {report['scenario']} ===")
    s = report["summary"]
    print(f"features={s['n_features']}, bins={s['n_bins']}, "
          f"smoothing alpha={s['smoothing_alpha']}")
    for f in report["features"]:
        m = f["metrics"]
        miss = f["missing_rate"]
        nm = f["non_missing"]
        w1 = m["wasserstein_1"]
        w1_s = "n/a" if w1 is None else f"{w1:.3f}"
        print(f"  {f['feature']:>11s}  PSI={m['psi']:.4f} "
              f"({m['psi_band']:>11s})  JS={m['js_divergence']:.4f} "
              f"TV={m['total_variation']:.3f}  W1={w1_s:>7s}  "
              f"missing {miss['baseline']:.0%}->{miss['current']:.0%}  "
              f"n_cur={nm['current']}")
        for w in f["flags"]["warnings"]:
            print(f"      warning: {w}")
    auc = report["model_auc"]
    print(f"  model ROC-AUC baseline={auc['baseline']} current={auc['current']}")
    print("  (PSI bands are heuristic conventions, not significance tests)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=SCENARIOS, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    scenarios = (args.scenario,) if args.scenario else SCENARIOS
    reports = [_run(name, seed=args.seed) for name in scenarios]
    if args.json:
        print(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2))
    else:
        for report in reports:
            _print_human(report)


if __name__ == "__main__":
    main()
