"""Tunable analysis thresholds, all explicit and overridable per request."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalyzerConfig:
    # sample counts
    min_total_samples: int = 8
    min_segment_points: int = 6
    min_changepoint_side: int = 8
    min_drift_change_side: int = 12
    max_changepoint_depth: int = 2

    # RTT outlier rejection (median/MAD)
    rtt_z_threshold: float = 3.5

    # confidence gates: uncertainty above these => status "uncertain"
    offset_uncertainty_max_s: float = 0.05
    drift_uncertainty_max_rel: float = 2e-3  # half-width / beta (0.2 %)
    drift_uncertainty_max_abs: float = 1e-5  # half-width in s per counter tick

    # counter wrap / restart disambiguation
    restart_zero_fraction: float = 0.05  # c_after <= 5 % of modulus looks like 0
    wrap_time_tol_mad: float = 5.0  # wrap consistency tolerance (MAD units)
    wrap_time_tol_rtt: float = 2.0  # ... plus this many median RTTs
    wrap_max_turns: int = 1024
