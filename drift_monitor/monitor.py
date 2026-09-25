"""Drift monitor: fit baseline bins once, score current windows many times.

The monitor owns one :class:`~drift_monitor.binning.FixedBinner` per
numeric feature. Scoring compares bucket counts between the frozen
baseline and the current window and emits PSI plus supporting metrics.

Important caveats carried into every report:

* PSI bands are heuristic conventions, not statistical significance.
* Small windows produce noisy PSI; ``non_missing`` counts are always
  reported and small samples are flagged.
* An all-missing window is scored honestly: PSI against the baseline
  missing-rate bucket is finite (with smoothing), but the numeric-bucket
  metrics become uninformative, which is surfaced as a warning.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .binning import FixedBinner, bucket_labels
from .metrics import (psi, js_divergence, total_variation, wasserstein_1,
                      psi_band, PSI_STABLE, PSI_WARNING)

# Below this many non-missing observations in the *current* window, PSI is
# dominated by sampling noise and the result is explicitly flagged.
SMALL_SAMPLE_N = 100


@dataclass
class FeatureDrift:
    feature: str
    psi: float
    psi_band: str
    js_divergence: float
    total_variation: float
    wasserstein_1: float  # NaN when not computable (all missing)
    missing_rate_baseline: float
    missing_rate_current: float
    non_missing_baseline: int
    non_missing_current: int
    baseline_counts: list[int]
    current_counts: list[int]
    bucket_labels: list[str]
    small_sample: bool
    all_missing_current: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "feature": self.feature,
            "metrics": {
                "psi": _json_float(self.psi),
                "psi_band": self.psi_band,
                "js_divergence": _json_float(self.js_divergence),
                "total_variation": _json_float(self.total_variation),
                "wasserstein_1": _json_float(self.wasserstein_1),
            },
            "missing_rate": {
                "baseline": self.missing_rate_baseline,
                "current": self.missing_rate_current,
            },
            "non_missing": {
                "baseline": self.non_missing_baseline,
                "current": self.non_missing_current,
            },
            "buckets": {
                "labels": self.bucket_labels,
                "baseline_counts": self.baseline_counts,
                "current_counts": self.current_counts,
            },
            "flags": {
                "small_sample": self.small_sample,
                "all_missing_current": self.all_missing_current,
                "warnings": self.warnings,
            },
        }


@dataclass
class DriftResult:
    features: list[FeatureDrift]
    alpha: float
    n_bins: int

    def to_dict(self) -> dict:
        return {
            "summary": {
                "n_features": len(self.features),
                "n_bins": self.n_bins,
                "smoothing_alpha": self.alpha,
                "max_psi": _json_float(max((f.psi for f in self.features),
                                           default=0.0)),
                "features_flagged": [
                    f.feature for f in self.features
                    if f.psi_band != "stable"
                ],
                "psi_thresholds": {
                    "stable_below": PSI_STABLE,
                    "significant_at_or_above": PSI_WARNING,
                    "note": (
                        "Heuristic rule-of-thumb bands from credit-risk "
                        "practice; not statistical significance tests."
                    ),
                },
            },
            "features": [f.to_dict() for f in self.features],
        }


def _json_float(x: float) -> float | None:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return float(x)


class DriftMonitor:
    """Fit fixed bins on a baseline table and score current-window tables.

    Parameters
    ----------
    n_bins, strategy:
        Forwarded to :class:`FixedBinner`.
    alpha:
        Laplace smoothing applied before PSI (default Jeffreys-Perks 0.5).
    small_sample_n:
        Current-window non-missing threshold for the small-sample flag.
    """

    def __init__(self, n_bins: int = 10, strategy: str = "quantile",
                 alpha: float = 0.5, small_sample_n: int = SMALL_SAMPLE_N) -> None:
        self.n_bins = n_bins
        self.strategy = strategy
        self.alpha = alpha
        self.small_sample_n = small_sample_n
        self.feature_names: list[str] = []
        self._binners: dict[str, FixedBinner] = {}
        self._baseline_counts: dict[str, np.ndarray] = {}
        self._baseline_total: dict[str, int] = {}

    # ------------------------------------------------------------------ fit
    def fit(self, data: dict[str, np.ndarray]) -> "DriftMonitor":
        """Learn one fixed binner per feature on baseline arrays."""
        self.feature_names = list(data.keys())
        self._binners = {}
        self._baseline_counts = {}
        self._baseline_total = {}
        for name in self.feature_names:
            x = np.asarray(data[name], dtype=np.float64).reshape(-1)
            binner = FixedBinner(n_bins=self.n_bins, strategy=self.strategy)
            binner.fit(x)
            counts = binner.counts(x)
            self._binners[name] = binner
            self._baseline_counts[name] = counts
            self._baseline_total[name] = int(x.size)
        return self

    # ---------------------------------------------------------------- score
    def score(self, data: dict[str, np.ndarray]) -> DriftResult:
        if not self._binners:
            raise RuntimeError("monitor must be fitted before scoring")
        unknown = set(data) - set(self.feature_names)
        missing = set(self.feature_names) - set(data)
        if unknown or missing:
            raise ValueError(
                f"feature mismatch (unknown={sorted(unknown)}, "
                f"missing={sorted(missing)}); expected {self.feature_names}"
            )
        results: list[FeatureDrift] = []
        for name in self.feature_names:
            results.append(self._score_feature(name, np.asarray(data[name])))
        return DriftResult(features=results, alpha=self.alpha, n_bins=self.n_bins)

    def _score_feature(self, name: str, x: np.ndarray) -> FeatureDrift:
        x = x.astype(np.float64, copy=False).reshape(-1)
        binner = self._binners[name]
        base_counts = self._baseline_counts[name]
        cur_counts = binner.counts(x)

        n_base = self._baseline_total[name]
        n_cur = int(x.size)
        miss_b = float(base_counts[-1]) / n_base if n_base else 0.0
        miss_c = float(cur_counts[-1]) / n_cur if n_cur else 0.0
        nonmiss_b = n_base - int(base_counts[-1])
        nonmiss_c = n_cur - int(cur_counts[-1])

        p, q = None, None  # noqa: F841 (kept for readability of the flow)
        value_psi = psi(base_counts, cur_counts, alpha=self.alpha)
        value_js = js_divergence(base_counts, cur_counts, alpha=0.0)
        value_tv = total_variation(base_counts, cur_counts, alpha=0.0)
        value_w1 = wasserstein_1(base_counts, cur_counts, binner.edges)

        warnings: list[str] = []
        small_sample = 0 < nonmiss_c < self.small_sample_n
        all_missing = n_cur > 0 and nonmiss_c == 0
        empty_window = n_cur == 0
        if small_sample:
            warnings.append(
                f"current window has only {nonmiss_c} non-missing values "
                f"(< {self.small_sample_n}); PSI is noisy, do not over-interpret"
            )
        if all_missing:
            warnings.append(
                "current window is entirely missing; numeric-bucket drift "
                "metrics are uninformative, only the missing-rate shift is real"
            )
        if empty_window:
            warnings.append("current window is empty; metrics are undefined")
        if int(cur_counts[-1]) == 0 and int(base_counts[-1]) == 0:
            # Missing bucket is structurally zero on both sides; PSI still
            # well-defined but worth noting that smoothing never kicks in there.
            pass
        zero_base = [
            lbl for lbl, c in zip(bucket_labels(binner), base_counts)
            if c == 0
        ]
        zero_cur = [
            lbl for lbl, c in zip(bucket_labels(binner), cur_counts)
            if c == 0
        ]
        if zero_base or zero_cur:
            warnings.append(
                "empty buckets present "
                f"(baseline: {zero_base or 'none'}, current: {zero_cur or 'none'}); "
                f"Laplace smoothing alpha={self.alpha} keeps PSI finite"
            )

        return FeatureDrift(
            feature=name,
            psi=value_psi,
            psi_band=psi_band(value_psi) if not empty_window else "undefined",
            js_divergence=value_js,
            total_variation=value_tv,
            wasserstein_1=value_w1,
            missing_rate_baseline=miss_b,
            missing_rate_current=miss_c,
            non_missing_baseline=nonmiss_b,
            non_missing_current=nonmiss_c,
            baseline_counts=[int(v) for v in base_counts],
            current_counts=[int(v) for v in cur_counts],
            bucket_labels=bucket_labels(binner),
            small_sample=small_sample,
            all_missing_current=all_missing,
            warnings=warnings,
        )

    # ---------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        return {
            "n_bins": self.n_bins,
            "strategy": self.strategy,
            "alpha": self.alpha,
            "small_sample_n": self.small_sample_n,
            "features": [
                {
                    "name": name,
                    "binner": self._binners[name].to_dict(),
                    "baseline_counts": [int(v) for v in self._baseline_counts[name]],
                    "baseline_total": self._baseline_total[name],
                }
                for name in self.feature_names
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "DriftMonitor":
        mon = cls(
            n_bins=int(payload["n_bins"]),
            strategy=payload["strategy"],
            alpha=float(payload["alpha"]),
            small_sample_n=int(payload.get("small_sample_n", SMALL_SAMPLE_N)),
        )
        mon.feature_names = [f["name"] for f in payload["features"]]
        mon._binners = {
            f["name"]: FixedBinner.from_dict(f["binner"])
            for f in payload["features"]
        }
        mon._baseline_counts = {
            f["name"]: np.asarray(f["baseline_counts"], dtype=np.int64)
            for f in payload["features"]
        }
        mon._baseline_total = {
            f["name"]: int(f["baseline_total"]) for f in payload["features"]
        }
        return mon
