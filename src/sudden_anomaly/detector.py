"""Streaming pulse-anomaly detector based on sliding robust statistics.

Causality contract
------------------
Every decision for sample ``x[t]`` uses *only* samples observed at or before
``t`` (and only strictly earlier samples for the statistics: the decision for
``x[t]`` is computed from the mean/scale already summarising ``x[0..t-1]``, and
only then is ``x[t]`` folded into the state).  No future sample is ever read,
and the same scalar code path serves both point-by-point and chunked calls, so
both invocation styles produce bit-identical decisions.

Decision rule (after warm-up)::

    pulse_z[t] = |x[t] - median(window)| / (k * MAD(window))
    shift_z[t] = |median(recent R) - median(older baseline)| / (k * MAD(baseline))
    decision_t = ANOMALY if max(pulse_z[t], shift_z[t]) > threshold else NORMAL

``pulse_z`` catches isolated impulses; the robust two-window ``shift_z``
catches level steps and slow drifts, which a single sliding-window z-score
cannot see because a monotone trend is absorbed into the window's own centre
and spread. Both statistics are computed from windows containing *only*
samples earlier than ``t`` (``window`` is updated after the decision).

    window:   trailing ``window_size`` observed samples (past only)
    recent:   its last ``recent_size`` samples
    baseline: window minus recent (needs >= min_history samples)

Missing-sample policy
---------------------
Missing values (``NaN``) are never imputed: they do not update the statistics
and receive decision ``NOT_DECIDED``.  Gaps do not consume window capacity.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Deque, Optional, Sequence

import numpy as np

# Decision labels -------------------------------------------------------------
DECISION_NORMAL = "NORMAL"
DECISION_ANOMALY = "ANOMALY"
DECISION_WARMING = "WARMING"
DECISION_NOT_DECIDED = "NOT_DECIDED"

DEFAULT_THRESHOLD = 5.0
DEFAULT_WINDOW_SIZE = 512
DEFAULT_MIN_HISTORY = 16
DEFAULT_PRIME_SIZE = 64
DEFAULT_RECENT_SIZE = 32
DEFAULT_RECENT_MIN = 8
DEFAULT_SHIFT_PERSIST = 3
DEFAULT_SCALE_ESTIMATOR = "mad"
DEFAULT_EPSILON_SCALE = 1e-9
# Hampel's consistency constant: 1.4826 makes MAD a consistent estimator of
# the standard deviation for Gaussian data.
HAMPEL_K = 1.4826
# Asymptotic standard error of a sample median of n Gaussian samples is
# 1.253 * sigma / sqrt(n); used to normalise the two-window centre shift.
MEDIAN_ARE_FACTOR = 1.253

_VALID_SCALE_ESTIMATORS = ("mad", "std")


class DetectorConfigError(ValueError):
    """Raised when detector parameters are invalid."""


def _median_last(x: np.ndarray, axis: int = -1) -> np.ndarray:
    """Median along ``axis`` (kept swap-in-able for faster medians)."""
    return np.median(x, axis=axis)


@dataclass(frozen=True)
class DetectorConfig:
    """Detector parameters.

    Attributes:
        threshold: decision threshold on the robust z-scores (strict ``>``).
        window_size: trailing window length counted in *observed* samples.
        min_history: minimum observed samples in the baseline part of the
            window before NORMAL / ANOMALY decisions are emitted.
        prime_size: number of observed samples used to build the initial
            window before the first decision is attempted.
        recent_size: length of the recent sub-window whose centre is compared
            against the older baseline to detect level shifts/drifts.
        recent_min: minimum observed samples the recent sub-window needs for
            the shift statistic to be used.
        shift_persist: number of consecutive samples whose shift score must
            exceed the threshold before a level-shift alarm is raised. The
            pulse statistic has no persistence (impulses alarm immediately);
            persistence only suppresses correlated noise excursions of the
            two-window statistic.
        scale_estimator: ``"mad"`` (Hampel median absolute deviation) or
            ``"std"`` (sample standard deviation).
        center: ``"median"`` (robust) or ``"mean"``.
        epsilon_scale: floor on the spread to avoid division by zero on
            constant signals.
    """

    threshold: float = DEFAULT_THRESHOLD
    window_size: int = DEFAULT_WINDOW_SIZE
    min_history: int = DEFAULT_MIN_HISTORY
    prime_size: int = DEFAULT_PRIME_SIZE
    recent_size: int = DEFAULT_RECENT_SIZE
    recent_min: int = DEFAULT_RECENT_MIN
    shift_persist: int = DEFAULT_SHIFT_PERSIST
    scale_estimator: str = DEFAULT_SCALE_ESTIMATOR
    center: str = "median"
    epsilon_scale: float = DEFAULT_EPSILON_SCALE

    def __post_init__(self) -> None:
        if not isinstance(self.threshold, (int, float)) or not math.isfinite(
            float(self.threshold)
        ):
            raise DetectorConfigError("threshold must be a finite number")
        if self.threshold <= 0:
            raise DetectorConfigError("threshold must be > 0")
        if not isinstance(self.window_size, int) or self.window_size < 2:
            raise DetectorConfigError("window_size must be an int >= 2")
        if not isinstance(self.min_history, int) or self.min_history < 2:
            raise DetectorConfigError("min_history must be an int >= 2")
        if not isinstance(self.prime_size, int) or self.prime_size < 2:
            raise DetectorConfigError("prime_size must be an int >= 2")
        if not (self.min_history + self.recent_min <= self.prime_size
                <= self.window_size):
            raise DetectorConfigError(
                "require min_history + recent_min <= prime_size <= window_size"
            )
        if not isinstance(self.recent_size, int) or not (
            self.recent_min <= self.recent_size
            and self.recent_size + self.min_history <= self.window_size
        ):
            raise DetectorConfigError(
                "recent_size must satisfy recent_min <= recent_size and "
                "recent_size + min_history <= window_size"
            )
        if not isinstance(self.recent_min, int) or self.recent_min < 2:
            raise DetectorConfigError("recent_min must be an int >= 2")
        if not isinstance(self.shift_persist, int) or self.shift_persist < 1:
            raise DetectorConfigError("shift_persist must be an int >= 1")
        if self.scale_estimator not in _VALID_SCALE_ESTIMATORS:
            raise DetectorConfigError(
                f"scale_estimator must be one of {_VALID_SCALE_ESTIMATORS}"
            )
        if self.center not in ("median", "mean"):
            raise DetectorConfigError("center must be 'median' or 'mean'")
        if (
            not isinstance(self.epsilon_scale, (int, float))
            or not math.isfinite(float(self.epsilon_scale))
            or self.epsilon_scale <= 0
        ):
            raise DetectorConfigError("epsilon_scale must be a finite number > 0")


@dataclass(frozen=True)
class SampleResult:
    """Result for a single sample."""

    decision: str
    score: float  # max(pulse_score, shift_score)
    pulse_score: float
    shift_score: float
    center: float
    scale: float
    n_observed: int


@dataclass(frozen=True)
class DetectionResult:
    """Result of an offline/streamed run over a signal.

    All arrays are aligned by input index (including positions marked as
    missing), which makes block-wise concatenation directly comparable.
    """

    decisions: np.ndarray  # dtype '<U11'
    scores: np.ndarray  # max of the two scores, NaN where no decision
    pulse_scores: np.ndarray  # single-sample robust z (impulses)
    shift_scores: np.ndarray  # two-window centre shift z (steps/drift)
    centers: np.ndarray  # baseline centre used at each position (NaN if none)
    scales: np.ndarray  # baseline spread used at each position (NaN if none)
    n_observed: np.ndarray  # observed-sample count *after* the position
    missing_rate: float
    n_anomalies: int
    anomaly_indices: np.ndarray

    def as_dict(self) -> dict:
        def _lst(a: np.ndarray) -> list:
            return [None if math.isnan(v) else float(v) for v in a]

        return {
            "decisions": self.decisions.tolist(),
            "scores": _lst(self.scores),
            "pulse_scores": _lst(self.pulse_scores),
            "shift_scores": _lst(self.shift_scores),
            "centers": _lst(self.centers),
            "scales": _lst(self.scales),
            "n_observed": self.n_observed.tolist(),
            "missing_rate": self.missing_rate,
            "n_anomalies": int(self.n_anomalies),
            "anomaly_indices": self.anomaly_indices.tolist(),
        }


class StreamingAnomalyDetector:
    """Incremental sliding-window robust anomaly detector.

    Usage::

        det = StreamingAnomalyDetector(DetectorConfig(window_size=128))
        out = det.feed(x_chunk)          # call once, or repeatedly per block
        out.decisions                    # aligned to every fed sample
    """

    def __init__(
        self,
        config: Optional[DetectorConfig] = None,
        *,
        median_fn: Optional[Callable[..., np.ndarray]] = None,
    ) -> None:
        self.config = config or DetectorConfig()
        self._median_fn: Callable[..., np.ndarray] = median_fn or _median_last

        # Window holds *observed* (non-missing) samples only.
        self._window: Deque[float] = deque(maxlen=self.config.window_size)
        self._seen = 0  # observed samples processed in total
        self._fed = 0  # samples fed (including missing) in total
        self._n_missing = 0
        # Consecutive (observed, no-gap) samples whose shift score exceeds
        # the threshold; a missing sample breaks the streak.
        self._shift_streak = 0

    # -- public API ----------------------------------------------------------

    @property
    def n_observed(self) -> int:
        return self._seen

    def feed(self, x: np.ndarray | Sequence[float]) -> DetectionResult:
        """Feed one sample or a contiguous block; state is updated in order."""
        arr = np.asarray(x, dtype=np.float64).reshape(-1)
        n = arr.size
        decisions = np.empty(n, dtype="<U11")
        scores = np.full(n, np.nan)
        pulse_scores = np.full(n, np.nan)
        shift_scores = np.full(n, np.nan)
        centers = np.full(n, np.nan)
        scales = np.full(n, np.nan)
        nobs = np.zeros(n, dtype=np.int64)
        for i, value in enumerate(arr):
            r = self._feed_scalar(value)
            decisions[i] = r.decision
            scores[i] = r.score
            pulse_scores[i] = r.pulse_score
            shift_scores[i] = r.shift_score
            centers[i] = r.center
            scales[i] = r.scale
            nobs[i] = r.n_observed
        return self._package(
            decisions, scores, pulse_scores, shift_scores,
            centers, scales, nobs, n,
        )

    def feed_many(
        self, blocks: Sequence[np.ndarray | Sequence[float]]
    ) -> DetectionResult:
        """Feed several blocks (e.g. simulated packet arrivals) at once."""
        results = [self.feed(block) for block in blocks]
        return self._stitch(results) if results else _empty_result()

    @staticmethod
    def stitch(results: Sequence[DetectionResult]) -> DetectionResult:
        """Concatenate per-block results into one index-aligned result."""
        return StreamingAnomalyDetector._stitch(results)

    # -- core scalar path ----------------------------------------------------

    def _feed_scalar(self, value: float) -> SampleResult:
        self._fed += 1
        if math.isnan(value):
            # Missing: no imputation, no statistic update, no decision.
            # The gap also breaks the persistence streak.
            self._n_missing += 1
            self._shift_streak = 0
            return SampleResult(
                DECISION_NOT_DECIDED, np.nan, np.nan, np.nan,
                np.nan, np.nan, self._seen,
            )
        if not math.isfinite(value):
            raise ValueError(
                "observed samples must be finite (NaN means 'missing'); "
                f"got {value!r} at position {self._fed - 1}"
            )

        result = self._decide(value)
        self._update(value)
        # Report the observed count *after* folding in this sample.
        return replace(result, n_observed=self._seen)

    def _segment_stats(
        self, seg: np.ndarray
    ) -> tuple[float, float]:
        """(centre, spread) of one observed-sample segment."""
        med = float(self._median_fn(seg))
        center = med if self.config.center == "median" else float(seg.mean())
        if self.config.scale_estimator == "std":
            return center, float(seg.std(ddof=0))
        mad = float(self._median_fn(np.abs(seg - med)))
        return center, HAMPEL_K * mad

    def _decide(self, value: float) -> SampleResult:
        """Decide using statistics over strictly earlier samples only."""
        cfg = self.config
        n = len(self._window)
        if (
            self._seen < cfg.prime_size
            or n < cfg.min_history + cfg.recent_min
        ):
            return SampleResult(
                DECISION_WARMING, np.nan, np.nan, np.nan,
                np.nan, np.nan, self._seen,
            )

        arr = np.asarray(self._window, dtype=np.float64)
        n_recent = min(cfg.recent_size, n - cfg.min_history)
        n_base = n - n_recent
        if n_base < cfg.min_history or n_recent < cfg.recent_min:
            return SampleResult(
                DECISION_WARMING, np.nan, np.nan, np.nan,
                np.nan, np.nan, self._seen,
            )
        base_seg = arr[:n_base]
        recent_seg = arr[n_base:]

        center_all, scale_all = self._segment_stats(arr)
        center_base, scale_base = self._segment_stats(base_seg)
        center_recent, _ = self._segment_stats(recent_seg)

        # 1) Pulse score: this sample vs the whole trailing window.
        pulse_score = abs(value - center_all) / max(
            scale_all, cfg.epsilon_scale
        )
        # 2) Shift score: recent centre vs older baseline centre.  The scale
        #    is taken from the larger, stable baseline segment only; using the
        #    short recent segment's noisy MAD as a denominator makes the ratio
        #    heavy-tailed and inflates false alarms.
        shift_se = max(
            MEDIAN_ARE_FACTOR
            * scale_base
            * math.sqrt(1.0 / n_base + 1.0 / n_recent),
            cfg.epsilon_scale,
        )
        shift_score = abs(center_recent - center_base) / shift_se

        pulse_alarm = pulse_score > cfg.threshold
        if shift_score > cfg.threshold:
            self._shift_streak += 1
        else:
            self._shift_streak = 0
        shift_alarm = self._shift_streak >= cfg.shift_persist

        score = max(pulse_score, shift_score)
        decision = (
            DECISION_ANOMALY if (pulse_alarm or shift_alarm) else DECISION_NORMAL
        )
        return SampleResult(
            decision, score, pulse_score, shift_score,
            center_all, scale_all, self._seen,
        )

    def _update(self, value: float) -> None:
        """Append an observed sample; the deque evicts the oldest itself."""
        self._window.append(value)
        self._seen += 1

    # -- packaging -----------------------------------------------------------

    def _package(
        self,
        decisions: np.ndarray,
        scores: np.ndarray,
        pulse_scores: np.ndarray,
        shift_scores: np.ndarray,
        centers: np.ndarray,
        scales: np.ndarray,
        nobs: np.ndarray,
        n: int,
    ) -> DetectionResult:
        anomaly_idx = np.flatnonzero(decisions == DECISION_ANOMALY).astype(np.int64)
        return DetectionResult(
            decisions=decisions,
            scores=scores,
            pulse_scores=pulse_scores,
            shift_scores=shift_scores,
            centers=centers,
            scales=scales,
            n_observed=nobs,
            missing_rate=(self._n_missing / self._fed if self._fed else 0.0),
            n_anomalies=int(anomaly_idx.size),
            anomaly_indices=anomaly_idx,
        )

    @staticmethod
    def _stitch(results: Sequence[DetectionResult]) -> DetectionResult:
        decisions = np.concatenate([r.decisions for r in results])
        scores = np.concatenate([r.scores for r in results])
        pulse_scores = np.concatenate([r.pulse_scores for r in results])
        shift_scores = np.concatenate([r.shift_scores for r in results])
        centers = np.concatenate([r.centers for r in results])
        scales = np.concatenate([r.scales for r in results])
        nobs = np.concatenate([r.n_observed for r in results])
        offset = 0
        idx = []
        for res in results:
            idx.append(res.anomaly_indices + offset)
            offset += res.decisions.size
        anomaly_indices = (
            np.concatenate(idx).astype(np.int64) if idx else np.empty(0, np.int64)
        )
        n_total = decisions.size
        n_missing = int(np.count_nonzero(decisions == DECISION_NOT_DECIDED))
        return DetectionResult(
            decisions=decisions,
            scores=scores,
            pulse_scores=pulse_scores,
            shift_scores=shift_scores,
            centers=centers,
            scales=scales,
            n_observed=nobs,
            missing_rate=(n_missing / n_total if n_total else 0.0),
            n_anomalies=int(anomaly_indices.size),
            anomaly_indices=anomaly_indices,
        )


def _empty_result() -> DetectionResult:
    return DetectionResult(
        decisions=np.empty(0, dtype="<U11"),
        scores=np.empty(0, dtype=float),
        pulse_scores=np.empty(0, dtype=float),
        shift_scores=np.empty(0, dtype=float),
        centers=np.empty(0, dtype=float),
        scales=np.empty(0, dtype=float),
        n_observed=np.empty(0, dtype=np.int64),
        missing_rate=0.0,
        n_anomalies=0,
        anomaly_indices=np.empty(0, dtype=np.int64),
    )


def detect_offline(
    x: np.ndarray | Sequence[float],
    config: Optional[DetectorConfig] = None,
    *,
    block_size: Optional[int] = None,
    median_fn: Optional[Callable[..., np.ndarray]] = None,
) -> DetectionResult:
    """Run the causal detector over a whole signal, optionally in blocks.

    ``block_size`` only changes how samples are fed; the stitched result must
    be identical to the one-shot result (this is asserted in the test suite).
    """
    arr = np.asarray(x, dtype=np.float64)
    if arr.ndim != 1:
        raise DetectorConfigError("input signal must be 1-D")
    detector = StreamingAnomalyDetector(config, median_fn=median_fn)
    if block_size is None:
        return detector.feed(arr)
    if not isinstance(block_size, int) or block_size < 1:
        raise DetectorConfigError("block_size must be a positive int or None")
    blocks = [arr[i : i + block_size] for i in range(0, arr.size, block_size)]
    return detector.feed_many(blocks)
