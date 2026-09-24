"""Clock-drift estimation math.

Model
-----
For one continuous operating regime we fit a line

        host_time = slope * device_counter_unwrapped + intercept

using request/response observations.  No symmetry is assumed for the network
path.  Each observed counter point carries a *feasible host-time interval*
derived purely from causality:

* single-counter sample at c:        host_time(c) in [t0, t3]
* request-arrival stamp c_recv:      host_time(c_recv) >= t0
* response-departure stamp c_send:   host_time(c_send) <= t3

The point estimate is a Huber-robust least-squares fit on interval midpoints
(classical, NTP-style); the *error bounds* combine bootstrap resampling with
the feasibility envelope, so asymmetric one-way delays widen and shift the
bounds instead of being averaged away.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Tunables (documented / testable)
# ---------------------------------------------------------------------------

RTT_FILTER_MAD_K = 2.5          # threshold = median(rtt) + k * MAD
RTT_FILTER_MIN_N = 7            # do not filter below this sample count
MIN_POINTS_CALIBRATED = 6       # minimum RTT-surviving samples for a published fit
TIME_SPAN_VS_RTT = 2.0          # host-time span must exceed k * median RTT
BOOTSTRAP_ITERS = 300
BOOTSTRAP_MIN_DISTINCT = 3      # distinct resampled samples required per refit
HUBER_K = 1.345
FORWARD_JUMP_FACTOR = 10.0      # counter advance > k * expected => forward jump
WRAP_RATE_TOL_REL = 0.25        # unwrapped advance within 25% of expected => wrap
CHANGEPOINT_MIN_SIDE = 6
# Calibrated against real round-trip captures: control runs (no break) stayed
# below F=3.6, genuine breaks above F=4.8. Use the F (likelihood-ratio style)
# statistic as the noise test, and separately require a *material* slope
# change (>100 ppm relative) so an arbitrarily tiny but high-n significant
# slope wiggle is not reported as a drift change.
CHANGEPOINT_F_THRESHOLD = 5.0
CHANGEPOINT_SLOPE_SE = 8.0      # retained for reference; no longer a hard gate
CHANGEPOINT_MIN_REL_SLOPE = 1e-4  # |Δslope|/slope must exceed 100 ppm
MAX_DRIFT_SPLITS = 2


class Observation:
    __slots__ = ("t0", "t3", "c_recv", "c_send", "seq")

    def __init__(self, t0: float, t3: float, c_recv: float,
                 c_send: Optional[float], seq: Optional[int]):
        self.t0 = float(t0)
        self.t3 = float(t3)
        self.c_recv = float(c_recv)
        self.c_send = None if c_send is None else float(c_send)
        self.seq = seq

    @property
    def rtt(self) -> float:
        return self.t3 - self.t0


@dataclass
class Point:
    """Counter value c with feasible host-time interval [lo, hi].

    ``anchor`` is the host time used for *point* estimation.  For a
    one-sided causality bound (lo=-inf or hi=+inf) the bound endpoint alone
    is a biased, high-variance anchor; the round-trip window centre is the
    minimax representative of where the true instant lies, so both stamps
    of a dual-stamped sample share it.  The rigorous [lo, hi] interval is
    still what drives the error envelope — anchors never tighten the bounds.
    """

    c: float
    lo: float
    hi: float
    sample_id: int  # points from the same sample share an id (bootstrap groups)
    anchor: Optional[float] = None


@dataclass
class FitResult:
    status: str
    n_input: int
    n_used: int
    n_rtt_filtered: int
    slope: Optional[float] = None
    intercept: Optional[float] = None
    slope_ci: Optional[tuple[float, float]] = None
    intercept_ci: Optional[tuple[float, float]] = None
    rtt_median: Optional[float] = None
    rtt_threshold: Optional[float] = None
    c_min: Optional[float] = None
    c_max: Optional[float] = None
    t_min: Optional[float] = None
    t_max: Optional[float] = None
    reason: Optional[str] = None
    # bootstrapped (slope, intercept) pairs, kept for envelope composition
    boot: Optional[np.ndarray] = field(default=None, repr=False)
    points: Optional[list[Point]] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Elementary linear algebra
# ---------------------------------------------------------------------------


def _ols(c: np.ndarray, y: np.ndarray, w: Optional[np.ndarray] = None
         ) -> tuple[float, float]:
    """Weighted least squares line y = slope*c + intercept."""
    if w is None:
        w = np.ones_like(c)
    sw = float(np.sum(w))
    mc = float(np.sum(w * c) / sw)
    my = float(np.sum(w * y) / sw)
    d = c - mc
    den = float(np.sum(w * d * d))
    if den <= 0.0 or not math.isfinite(den):
        # Singular: fall back to constant predictor at the weighted mean.
        return 0.0, my
    slope = float(np.sum(w * d * (y - my)) / den)
    intercept = my - slope * mc
    return slope, intercept


def _midpoint(p: Point) -> float:
    if math.isfinite(p.lo) and math.isfinite(p.hi):
        return 0.5 * (p.lo + p.hi)
    # One-sided causality bound: use the minimax RTT-window anchor.
    if p.anchor is not None:
        return p.anchor
    if math.isfinite(p.lo):
        return p.lo
    return p.hi


def _huber_fit(points: list[Point]) -> tuple[float, float]:
    """Robust fit against interval midpoints."""
    c = np.array([p.c for p in points], dtype=float)
    y = np.array([_midpoint(p) for p in points], dtype=float)
    slope, intercept = _ols(c, y)
    # IRLS with Huber weights; scale from MAD of residuals.
    for _ in range(20):
        r = y - (slope * c + intercept)
        med = float(np.median(r))
        mad = float(np.median(np.abs(r - med)))
        scale = 1.4826 * mad
        if scale <= 0.0 or not math.isfinite(scale):
            break
        u = np.abs(r - med) / scale
        w = np.where(u <= HUBER_K, 1.0, HUBER_K / np.maximum(u, 1e-12))
        s_new, b_new = _ols(c, y, w)
        if (abs(s_new - slope) < 1e-12 * max(1.0, abs(slope))
                and abs(b_new - intercept) < 1e-12 * max(1.0, abs(intercept))):
            slope, intercept = s_new, b_new
            break
        slope, intercept = s_new, b_new
    return slope, intercept


# ---------------------------------------------------------------------------
# RTT filtering
# ---------------------------------------------------------------------------


def rtt_filter(obs: list[Observation]) -> tuple[list[bool], float, float]:
    """Drop loose (large-RTT) samples.

    Returns (keep mask, median rtt, threshold).  Small samples are never
    filtered; the rule also guarantees a usable core survives.
    """
    n = len(obs)
    rtts = np.array([o.rtt for o in obs], dtype=float)
    med = float(np.median(rtts))
    if n < RTT_FILTER_MIN_N:
        return [True] * n, med, math.inf
    mad = float(np.median(np.abs(rtts - med)))
    threshold = med + RTT_FILTER_MAD_K * 1.4826 * mad
    keep = [r <= threshold for r in rtts]
    # Guarantee at least max(MIN_POINTS_CALIBRATED-2, ~60%) samples survive:
    guaranteed = max(MIN_POINTS_CALIBRATED - 2, int(math.ceil(0.6 * n)))
    if sum(keep) < min(guaranteed, n):
        order = np.argsort(rtts)
        keep = [False] * n
        for i in order[:guaranteed]:
            keep[int(i)] = True
        threshold = float(rtts[order[guaranteed - 1]])
    return keep, med, threshold


# ---------------------------------------------------------------------------
# Points construction
# ---------------------------------------------------------------------------


def make_points(obs: list[Observation], offsets: list[float],
                send_offsets: Optional[list[float]] = None) -> list[Point]:
    """Build feasible-interval points, applying per-sample unwrap offsets.

    ``offsets`` unwrap c_recv; ``send_offsets`` unwrap c_send (the two stamps
    can straddle a modulus boundary independently during processing time).
    """
    if send_offsets is None:
        send_offsets = offsets
    pts: list[Point] = []
    for i, (o, off, soff) in enumerate(zip(obs, offsets, send_offsets)):
        cr = o.c_recv + off
        centre = 0.5 * (o.t0 + o.t3)
        if o.c_send is None:
            # Instantaneous stamp somewhere inside the whole RTT window.
            pts.append(Point(cr, o.t0, o.t3, i, centre))
        else:
            # Causality gives one-sided bounds per direction — no symmetry —
            # but both stamps share the RTT-window centre as point anchor.
            pts.append(Point(cr, o.t0, math.inf, i, centre))
            pts.append(Point(o.c_send + soff, -math.inf, o.t3, i, centre))
    return pts


# ---------------------------------------------------------------------------
# Feasibility envelope + bootstrap uncertainty
# ---------------------------------------------------------------------------


def _envelope(points: list[Point], slope: float, c0: float) -> tuple[float, float]:
    """Tightest host-time interval at c0 for a line of given slope.

    A line host = slope*c + k is feasible iff  lo_i <= slope*c_i + k <= hi_i
    for every point, hence k in [max_i(lo_i - slope c_i), min_i(hi_i -
    slope c_i)].
    """
    k_lo = -math.inf
    k_hi = math.inf
    for p in points:
        if math.isfinite(p.lo):
            k_lo = max(k_lo, p.lo - slope * p.c)
        if math.isfinite(p.hi):
            k_hi = min(k_hi, p.hi - slope * p.c)
    return slope * c0 + k_lo, slope * c0 + k_hi


def predict_interval(fit: FitResult, c0: float,
                     alpha: float = 0.05) -> tuple[float, float, float]:
    """Return (point, lower, upper) host time at unwrapped counter c0."""
    assert fit.slope is not None and fit.intercept is not None
    point = fit.slope * c0 + fit.intercept
    if fit.boot is None or len(fit.boot) == 0 or fit.points is None:
        return point, point, point
    preds = fit.boot[:, 0] * c0 + fit.boot[:, 1]
    lo_stat = float(np.quantile(preds, alpha / 2))
    hi_stat = float(np.quantile(preds, 1 - alpha / 2))
    # Asymmetric, delay-driven bounds: envelope across bootstrap slopes.
    env_lo, env_hi = math.inf, -math.inf
    for s in fit.boot[:, 0]:
        el, eh = _envelope(fit.points, float(s), c0)
        env_lo = min(env_lo, el)
        env_hi = max(env_hi, eh)
    el0, eh0 = _envelope(fit.points, fit.slope, c0)
    env_lo = min(env_lo, el0)
    env_hi = max(env_hi, eh0)
    return point, min(lo_stat, env_lo), max(hi_stat, env_hi)


# ---------------------------------------------------------------------------
# Segment fitting
# ---------------------------------------------------------------------------


def fit_segment(obs: list[Observation], offsets: list[float],
                rng: np.random.Generator,
                rtt_keep: Optional[list[bool]] = None,
                send_offsets: Optional[list[float]] = None) -> FitResult:
    """Fit one continuous regime (obs already restricted to the segment)."""
    if send_offsets is None:
        send_offsets = offsets
    n = len(obs)
    if rtt_keep is None:
        rtt_keep, rtt_med, rtt_thr = rtt_filter(obs)
    else:
        rtts = [o.rtt for o in obs]
        rtt_med = float(np.median(rtts))
        rtt_thr = float(np.max([r for r, k in zip(rtts, rtt_keep) if k])) if any(rtt_keep) else math.inf
    kept = [o for o, k in zip(obs, rtt_keep) if k]
    kept_off = [u for u, k in zip(offsets, rtt_keep) if k]
    kept_send_off = [u for u, k in zip(send_offsets, rtt_keep) if k]
    points = make_points(kept, kept_off, kept_send_off)
    n_filtered = n - len(kept)

    base = FitResult(
        status="uncertain",
        n_input=n,
        n_used=len(kept),
        n_rtt_filtered=n_filtered,
        rtt_median=rtt_med,
        rtt_threshold=rtt_thr,
    )
    if len(kept) < 2:
        base.reason = f"only {len(kept)} sample(s) survive RTT filtering"
        return base

    c_arr = np.array([p.c for p in points])
    t_arr = np.array([[p.lo, p.hi] for p in points])
    base.c_min, base.c_max = float(np.min(c_arr)), float(np.max(c_arr))
    finite_t = t_arr[np.isfinite(t_arr)]
    base.t_min = float(np.min(finite_t))
    base.t_max = float(np.max(finite_t))

    if len(set(np.round(c_arr, 12))) < 2:
        base.reason = "all surviving samples share one counter value; slope unidentifiable"
        return base

    slope, intercept = _huber_fit(points)

    # Bootstrap: resample whole samples (groups of 1 or 2 points).
    groups: dict[int, list[Point]] = {}
    for p in points:
        groups.setdefault(p.sample_id, []).append(p)
    gids = np.array(sorted(groups), dtype=int)
    boots: list[tuple[float, float]] = []
    if len(gids) >= BOOTSTRAP_MIN_DISTINCT:
        for _ in range(BOOTSTRAP_ITERS):
            pick = rng.choice(gids, size=len(gids), replace=True)
            if len(np.unique(pick)) < BOOTSTRAP_MIN_DISTINCT:
                continue
            rp: list[Point] = []
            new_id = 0
            for gid in pick:
                for p in groups[int(gid)]:
                    rp.append(Point(p.c, p.lo, p.hi, new_id, p.anchor))
                new_id += 1
            try:
                s, b = _huber_fit(rp)
            except Exception:
                continue
            if math.isfinite(s) and math.isfinite(b):
                boots.append((s, b))

    res = FitResult(
        status="calibrated",
        n_input=n, n_used=len(kept), n_rtt_filtered=n_filtered,
        slope=slope, intercept=intercept,
        rtt_median=rtt_med, rtt_threshold=rtt_thr,
        c_min=base.c_min, c_max=base.c_max, t_min=base.t_min, t_max=base.t_max,
        boot=np.array(boots) if boots else None,
        points=points,
    )
    if boots:
        res.slope_ci = (
            float(np.quantile(res.boot[:, 0], 0.025)),
            float(np.quantile(res.boot[:, 0], 0.975)),
        )
        res.intercept_ci = (
            float(np.quantile(res.boot[:, 1], 0.025)),
            float(np.quantile(res.boot[:, 1], 0.975)),
        )

    # Evidence gates.
    if len(kept) < MIN_POINTS_CALIBRATED:
        res.status = "uncertain"
        res.reason = (
            f"only {len(kept)} usable samples (need >= {MIN_POINTS_CALIBRATED}); "
            "error bounds would be dominated by extrapolation"
        )
        return res
    host_span = res.t_max - res.t_min
    if host_span <= TIME_SPAN_VS_RTT * max(rtt_med, 1e-12):
        res.status = "uncertain"
        res.reason = (
            f"observation span {host_span:.3g}s is not > "
            f"{TIME_SPAN_VS_RTT}x median RTT {rtt_med:.3g}s; "
            "network noise dominates the drift signal"
        )
        return res
    if res.slope_ci is None or not all(math.isfinite(x) for x in res.slope_ci):
        res.status = "uncertain"
        res.reason = "bootstrap failed to produce finite slope confidence interval"
        return res
    return res


# ---------------------------------------------------------------------------
# Discontinuity classification + segmentation
# ---------------------------------------------------------------------------


@dataclass
class Event:
    type: str
    at_index: int                # index into the (sorted) observation list
    at_host_time: float
    evidence: str
    from_segment: Optional[int] = None
    to_segment: Optional[int] = None


@dataclass
class Segment:
    obs: list[Observation]
    offsets: list[float]         # unwrap offset per observation (c_recv)
    send_offsets: list[float]    # unwrap offset per observation (c_send)
    fit: Optional[FitResult] = None


def _local_rate(t: list[float], c: list[float]) -> Optional[float]:
    if len(t) < 3:
        return None
    rates = []
    for i in range(max(1, len(t) - 8), len(t)):
        dt = t[i] - t[i - 1]
        if dt > 0:
            rates.append((c[i] - c[i - 1]) / dt)
    if not rates:
        return None
    r = float(np.median(rates))
    return r if math.isfinite(r) and r > 0 else None


def _classify_backward(prev_raw: float, cur_raw: float, modulus: Optional[float],
                       expected: Optional[float]
                       ) -> tuple[Optional[str], int, str]:
    """Classify a backward raw-counter step.

    Returns (kind, wraps, evidence).  ``wraps`` is the modular period count
    to add when unwrapping; kind is one of wrap/reboot/time_jump/uncertain.

    A genuine rollover is identified *kinematically*: there must exist an
    integer k>=1 of periods such that ``cur_raw + k*M - prev_raw`` (the
    unwrapped advance) agrees with the advance predicted by the local counter
    rate over the elapsed time, within delay jitter.  Position relative to M
    alone is unreliable — a busy low-width counter can wrap from anywhere.
    """
    delta = cur_raw - prev_raw
    if modulus is None:
        # No modular arithmetic possible. A big backward drop with no rate
        # baseline is uncertain; with one, a near-zero drop is a reboot and a
        # modest step a time jump.
        if expected is None:
            return "uncertain", 0, (
                f"backward jump {delta:.6g} with no modulus and no rate "
                "baseline; cannot tell reboot from a time step")
        if cur_raw < 0.5 * max(prev_raw, 1.0) and -delta > 2.0 * max(expected, 0.0):
            return "reboot", 0, (
                f"counter reset from {prev_raw:.6g} to {cur_raw:.6g} with no "
                f"modulus; backward gap {-delta:.6g} dwarfs the expected "
                f"{expected:.6g} advance")
        return "time_jump", 0, (
            f"backward step {delta:.6g} inconsistent with a reset")

    if expected is not None and expected > 0:
        # Allow 25% slack for rate-estimation and RTT jitter in the advance.
        tol = WRAP_RATE_TOL_REL * expected
        k_est = max(1, int(round((expected - delta) / modulus)))
        best = None
        for k in range(max(1, k_est - 4), k_est + 5):
            adv = delta + k * modulus
            if adv <= 0:
                continue
            residual = abs(adv - expected)
            if residual <= tol and (best is None or residual < best[1]):
                best = (k, residual, adv)
        if best is not None:
            k, residual, adv = best
            return "wrap", k, (
                f"backward step {delta:.6g} unwraps consistently: adding "
                f"{k}x{modulus:g} gives advance {adv:.6g} vs the ~{expected:.6g} "
                f"expected over the interval (residual {residual:.3g})")
        # Rate known but no period count fits: a near-zero landing is a reboot.
        if cur_raw < 0.1 * modulus and prev_raw >= 0.25 * modulus:
            return "reboot", 0, (
                f"counter reset to near zero ({prev_raw:.6g}->{cur_raw:.6g}); "
                "no integer number of periods makes the advance rate-consistent")
        return "time_jump", 0, (
            f"backward step {delta:.6g} is not a rate-consistent rollover and "
            "not a near-zero reset")

    # No rate baseline yet (startup). A drop from the top of the range whose
    # single-period unwrap is a positive advance is optimistically treated as
    # one rollover so a usable rate can be established; reboots landing near
    # zero remain distinguishable once evidence accumulates.
    if prev_raw >= 0.5 * modulus and delta + modulus > 0:
        return "wrap", 1, (
            f"startup backward step {delta:.6g} from {prev_raw:.6g} near the "
            f"top of the 0..{modulus:g} range; assumed one rollover to seed "
            "the rate estimate")
    return "uncertain", 0, (
        f"backward step {delta:.6g} with no rate baseline and away from the "
        "rollover boundary; wrap vs reboot undecidable")


def unwrap_counter(obs: list[Observation], modulus: Optional[float],
                   nominal_rate: Optional[float] = None
                   ) -> tuple[list[float], Optional[float], list[tuple[int, int, float, float]]]:
    """Unwrap raw c_recv values into a monotonic counter.

    Returns (offsets, estimated_rate, wrap_gaps) where wrap_gaps lists
    ``(index, periods, advance, expected)`` for every gap that crossed one or
    more modulus periods.

    When wraps can occur more than once per sampling interval, adjacent raw
    deltas cannot reveal the rate.  Instead we *try* candidate rates near the
    nominal tick rate, unwrap deterministically for each, and keep the rate
    whose unwrapped counter is most self-consistent (largest R^2 vs host
    time).  A genuine reboot breaks the assumption and is handled by the
    caller via :func:`segment_observations`.
    """
    n = len(obs)
    offsets = [0.0] * n

    if modulus is None:
        return offsets, nominal_rate, []

    t = np.array([o.t0 for o in obs], dtype=float)
    raw = np.array([o.c_recv for o in obs], dtype=float)
    dt = np.diff(t)

    # Candidate rates: nominal rate with a wide ppm sweep. Crystal oscillators
    # are within a few hundred ppm, but allow ±1% to tolerate misconfiguration.
    if nominal_rate is None:
        nominal_rate = 1.0  # one counter unit per host-second, generic
    candidates = [nominal_rate * (1.0 + f * 1e-6)
                  for f in np.linspace(-10_000, 10_000, 81)]

    best_rate = nominal_rate
    best_score = -np.inf
    best_unwrapped = None

    for rate in candidates:
        un = np.empty(n)
        un[0] = raw[0]
        ok = True
        for j in range(1, n):
            if dt[j - 1] <= 0:
                un[j] = un[j - 1] + (raw[j] - raw[j - 1])
                continue
            expected = rate * dt[j - 1]
            k = int(round((expected - (raw[j] - raw[j - 1])) / modulus))
            k = max(0, k)
            un[j] = un[j - 1] + (raw[j] - raw[j - 1]) + k * modulus
            if un[j] < un[j - 1]:
                ok = False
        if not ok or np.any(np.diff(un) <= 0):
            continue
        # Linearity score: R^2 of unwrapped counter vs host time.
        if np.ptp(un) <= 0:
            continue
        slope = np.polyfit(t - t[0], un, 1)[0]
        resid = un - (slope * (t - t[0]) + un[0])
        ss_res = float(np.sum(resid ** 2))
        ss_tot = float(np.sum((un - un.mean()) ** 2))
        score = 1.0 - ss_res / max(ss_tot, 1e-300)
        if score > best_score:
            best_score = score
            best_rate = float(rate)
            best_unwrapped = un

    if best_unwrapped is None:
        return offsets, nominal_rate, []

    # Build offsets from the chosen unwrapped series.
    wrap_gaps: list[tuple[int, int, float, float]] = []
    for j in range(1, n):
        off = best_unwrapped[j] - raw[j]
        prev_off = best_unwrapped[j - 1] - raw[j - 1]
        periods = int(round((off - prev_off) / modulus))
        offsets[j] = float(off)
        if periods >= 1:
            advance = float(best_unwrapped[j] - best_unwrapped[j - 1])
            expected = best_rate * dt[j - 1] if dt[j - 1] > 0 else advance
            wrap_gaps.append((j, periods, advance, float(expected)))
    return offsets, float(best_rate), wrap_gaps


def _send_offsets(obs, offsets: list[float], modulus: Optional[float]) -> list[float]:
    """Derive c_send unwrap offsets from c_recv offsets (+1 period if the
    response-departure stamp wrapped past the arrival stamp)."""
    out = []
    for o, roff in zip(obs, offsets):
        if modulus is None or o.c_send is None:
            out.append(roff)
            continue
        periods = roff / modulus
        out.append((periods + (1.0 if o.c_send < o.c_recv else 0.0)) * modulus)
    return out


def _segment_within(obs: list[Observation], modulus: Optional[float],
                    nominal_rate: Optional[float]
                    ) -> tuple[list[Segment], list[Event], Optional[float]]:
    """Classify the gaps of an assumed-reboot-free series.

    Wraps are resolved globally (a gap may cross several periods); the series
    is then scanned for gaps whose unwrapped advance is grossly inconsistent
    with the fitted rate — those are reboots (counter reset near zero),
    forward time jumps, or unresolved/uncertain gaps, and each starts a new
    segment.  Returns segments, events and the rate estimated for this run.
    """
    if modulus is None:
        offsets = [0.0] * len(obs)
        # Rate from forward steps only.
        rates = [(b.c_recv - a.c_recv) / (b.t0 - a.t0)
                 for a, b in zip(obs, obs[1:])
                 if b.t0 > a.t0 and b.c_recv >= a.c_recv]
        rate = float(np.median(rates)) if rates else nominal_rate
        wrap_gaps = []
    else:
        offsets, rate, wrap_gaps = unwrap_counter(obs, modulus, nominal_rate)

    send_offsets = _send_offsets(obs, offsets, modulus)

    # Boundary scan over gaps, using the unwrapped advance vs expected.
    cuts: list[tuple[int, str, str]] = []  # (index, kind, evidence)
    wrap_idx = {j: (k, adv, exp) for (j, k, adv, exp) in wrap_gaps}
    for j in range(1, len(obs)):
        dt = obs[j].t0 - obs[j - 1].t0
        raw_step = obs[j].c_recv - obs[j - 1].c_recv
        if modulus is None and raw_step < 0:
            # An unbounded counter cannot roll over: a drop is a reset, a time
            # step, or (without rate evidence) genuinely uncertain.
            if rate is None or dt <= 0:
                kind = ("reboot"
                        if obs[j].c_recv < 0.5 * max(obs[j - 1].c_recv, 1.0)
                        else "uncertain")
                cuts.append((j, kind, (
                    f"counter dropped {raw_step:.6g} on an unbounded counter "
                    "with no rate baseline")))
                continue
            expected = rate * dt
            if obs[j].c_recv < 0.5 * max(obs[j - 1].c_recv, 1.0) and -raw_step > 2 * expected:
                cuts.append((j, "reboot", (
                    f"counter reset from {obs[j-1].c_recv:.6g} to "
                    f"{obs[j].c_recv:.6g} with no modulus; drop dwarfs the "
                    f"~{expected:.6g} expected advance")))
            else:
                cuts.append((j, "time_jump", (
                    f"backward step {raw_step:.6g} on an unbounded counter")))
            continue
        if dt <= 0 or rate is None:
            continue
        adv = (obs[j].c_recv + offsets[j]) - (obs[j - 1].c_recv + offsets[j - 1])
        expected = rate * dt
        if j in wrap_idx and raw_step < 0:
            k, wadv, wexp = wrap_idx[j]
            # Visible rollover (raw went backwards). A reset landing near zero
            # with a grossly inconsistent unwrap is handled below, not here.
            if abs(wadv - wexp) <= WRAP_RATE_TOL_REL * wexp:
                cuts.append((j, "wrap", (
                    f"raw counter rolled over ({obs[j-1].c_recv:.6g}->"
                    f"{obs[j].c_recv:.6g}); adding {k}x{modulus:g} gives "
                    f"{wadv:.6g}, matching ~{wexp:.6g} over {dt:.6g}s")))
                continue
        # Non-wrap gap (or an inconsistent wrap): discontinuity?
        if adv > FORWARD_JUMP_FACTOR * expected and adv > 0:
            cuts.append((j, "time_jump", (
                f"unwrapped advance {adv:.6g} > {FORWARD_JUMP_FACTOR:g}x the "
                f"{expected:.6g} expected over {dt:.6g}s")))
        elif adv < 0:
            cur_raw = obs[j].c_recv
            near_zero = (cur_raw < 0.1 * modulus) if modulus is not None else (
                cur_raw < 0.5 * max(obs[j - 1].c_recv, 1.0))
            if near_zero:
                cuts.append((j, "reboot", (
                    f"counter reset toward zero ({obs[j-1].c_recv:.6g}->"
                    f"{cur_raw:.6g}); backward unwrapped advance {adv:.6g}")))
            else:
                cuts.append((j, "uncertain", (
                    f"backward unwrapped advance {adv:.6g} inconsistent with a "
                    "rollover and not a near-zero reset")))
        elif abs(adv - expected) > max(WRAP_RATE_TOL_REL * expected, 0.0) and j in wrap_idx:
            cuts.append((j, "uncertain", (
                f"wrap-gap advance {adv:.6g} disagrees with ~{expected:.6g}")))

    # Build segments; wraps do not split, everything else does.
    segments: list[Segment] = []
    events: list[Event] = []
    start = 0
    cut_at = {j: (kind, ev) for (j, kind, ev) in cuts if kind != "wrap"}
    wrap_events = [(j, ev) for (j, kind, ev) in cuts if kind == "wrap"]
    boundaries = [0] + sorted(cut_at) + [len(obs)]
    for bi in range(len(boundaries) - 1):
        a, b = boundaries[bi], boundaries[bi + 1]
        base_off = offsets[a]
        seg_off = [off - base_off for off in offsets[a:b]]
        # Within a reboot-delimited regime the counter restarted near raw 0.
        segments.append(Segment(
            obs=obs[a:b],
            offsets=seg_off,
            send_offsets=[so - (send_offsets[a]) + seg_off[0]
                          for so in send_offsets[a:b]],
        ))
    for j, (kind, evtxt) in sorted(cut_at.items()):
        # locate segment index this cut begins
        seg_to = sum(1 for x in boundaries[1:-1] if x <= j)
        events.append(Event(type=kind, at_index=j, at_host_time=obs[j].t0,
                            evidence=evtxt,
                            from_segment=max(0, seg_to - 1), to_segment=seg_to))
    for j, evtxt in wrap_events:
        events.append(Event(type="wrap", at_index=j, at_host_time=obs[j].t0,
                            evidence=evtxt, from_segment=0, to_segment=0))
    events.sort(key=lambda e: e.at_host_time)
    # Fix wrap event segment refs after segmentation.
    nseg = len(segments)
    if nseg > 1:
        # map each sample index to its segment
        idx2seg = {}
        for si, (a, b) in enumerate(zip(boundaries[:-1], boundaries[1:])):
            for x in range(a, b):
                idx2seg[x] = si
        for e in events:
            if e.type == "wrap":
                s = idx2seg.get(e.at_index, 0)
                e.from_segment = e.to_segment = s
    return segments, events, rate


def segment_observations(obs: list[Observation],
                         modulus: Optional[float],
                         nominal_rate: Optional[float] = None
                         ) -> tuple[list[Segment], list[Event]]:
    """Split into continuous regimes and classify every discontinuity.

    Wrap vs reboot are distinguished kinematically.  A reboot restarts the
    counter near zero; to keep wrap unwrapping robust we first solve the
    no-reboot problem, detect reset gaps, and recurse on each reboot-free
    block, resetting the unwrap origin at every block.
    """
    if len(obs) <= 1:
        seg = Segment(obs=list(obs),
                      offsets=[0.0] * len(obs),
                      send_offsets=[0.0] * len(obs))
        return ([seg] if obs else []), []

    segments, events, _ = _segment_within(obs, modulus, nominal_rate)

    # Re-normalise offsets inside each segment so each regime starts near 0.
    for seg in segments:
        if not seg.obs:
            continue
        base_r = seg.offsets[0]
        seg.offsets = [o - base_r for o in seg.offsets]
        if seg.send_offsets:
            base_s = seg.send_offsets[0]
            seg.send_offsets = [o - base_s for o in seg.send_offsets]
    return segments, events



# ---------------------------------------------------------------------------
# Drift changepoint detection
# ---------------------------------------------------------------------------


def _sse(c: np.ndarray, y: np.ndarray) -> float:
    s, b = _ols(c, y)
    return float(np.sum((y - (s * c + b)) ** 2)), s, b


def detect_drift_changepoints(segment: Segment) -> list[int]:
    """Indices (within the segment) where the slope provably changes.

    A stringent F-test plus a slope-gap-vs-standard-error requirement keeps
    ordinary network jitter from being reported as a drift change.
    """
    cuts: list[int] = []

    # iterative (bounded) search over current sub-ranges
    ranges: list[tuple[int, int]] = [(0, len(segment.obs))]
    while len(cuts) < MAX_DRIFT_SPLITS and ranges:
        new_ranges: list[tuple[int, int]] = []
        for a, b in list(ranges):
            n = b - a
            if n < 2 * CHANGEPOINT_MIN_SIDE:
                continue
            obs_sub = segment.obs[a:b]
            off_sub = segment.offsets[a:b]
            soff_sub = segment.send_offsets[a:b]
            pts = make_points(obs_sub, off_sub, soff_sub)
            c = np.array([p.c for p in pts])
            y = np.array([_midpoint(p) for p in pts])
            sse0, _, _ = _sse(c, y)

            best: Optional[tuple[float, int, float, float]] = None
            for k in range(CHANGEPOINT_MIN_SIDE, n - CHANGEPOINT_MIN_SIDE):
                # split at sample boundary k; map to points by sample_id
                c1, y1, c2, y2 = [], [], [], []
                for p, yy in zip(pts, y):
                    (c1 if p.sample_id < k else c2).append(p.c)
                    (y1 if p.sample_id < k else y2).append(yy)
                if len(c1) < 2 or len(c2) < 2:
                    continue
                c1a, y1a = np.array(c1), np.array(y1)
                c2a, y2a = np.array(c2), np.array(y2)
                sse1, s1, _ = _sse(c1a, y1a)
                sse2, s2, _ = _sse(c2a, y2a)
                df_num, df_den = 2, max(n - 4, 1)
                f = ((sse0 - sse1 - sse2) / df_num) / max((sse1 + sse2) / df_den, 1e-300)
                if best is None or f > best[0]:
                    best = (f, k, s1, s2)
            if best is None:
                continue
            f, k, s1, s2 = best
            denom = max(abs(s1), abs(s2), 1e-300)
            material = abs(s1 - s2) > CHANGEPOINT_MIN_REL_SLOPE * denom
            if f > CHANGEPOINT_F_THRESHOLD and material:
                cuts.append(a + k)
                new_ranges.append((a, a + k))
                new_ranges.append((a + k, b))
        ranges = new_ranges

    cuts.sort()
    return cuts


def apply_drift_cuts(segments: list[Segment],
                     events: list[Event]) -> tuple[list[Segment], list[Event]]:
    """Split segments at detected drift changepoints and append events."""
    out: list[Segment] = []
    new_events: list[Event] = []
    out_idx = 0
    for seg in segments:
        cuts = detect_drift_changepoints(seg)
        if not cuts:
            out.append(seg)
            out_idx += 1
            continue
        base_idx = out_idx
        bounds = [0] + cuts + [len(seg.obs)]
        for j in range(len(bounds) - 1):
            a, b = bounds[j], bounds[j + 1]
            out.append(Segment(obs=seg.obs[a:b],
                               offsets=seg.offsets[a:b],
                               send_offsets=seg.send_offsets[a:b]))
            out_idx += 1
        for j, k in enumerate(cuts):
            new_events.append(Event(
                type="drift_change",
                at_index=-1,
                at_host_time=seg.obs[k].t0,
                evidence=(
                    "two-phase regression on either side of this point improves "
                    f"fit beyond F={CHANGEPOINT_F_THRESHOLD:g} and the slope "
                    f"change is material (>{CHANGEPOINT_MIN_REL_SLOPE*1e6:g} ppm)"
                ),
                from_segment=base_idx + j,
                to_segment=base_idx + j + 1,
            ))
    return out, sorted(events + new_events, key=lambda e: e.at_host_time)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


@dataclass
class SegmentOutput:
    fit: FitResult
    first_host_time: float
    last_host_time: float


def calibrate(observations: list[Observation],
              modulus: Optional[float],
              rng: Optional[np.random.Generator] = None,
              nominal_hz: Optional[float] = None
              ) -> tuple[list[SegmentOutput], list[Event], list[Segment]]:
    """Full pipeline: segment -> drift cuts -> per-segment robust fit.

    Observations are sorted by (t0, t3) internally; input order is untouched.
    ``nominal_hz`` is the device's expected counter frequency and anchors
    modular unwrapping when wraps occur more often than samples.
    """
    if rng is None:
        rng = np.random.default_rng(0x076A)
    obs = sorted(observations, key=lambda o: (o.t0, o.t3))
    segments, events = segment_observations(obs, modulus, nominal_hz)
    segments, events = apply_drift_cuts(segments, events)

    outputs: list[SegmentOutput] = []
    for seg in segments:
        fit = fit_segment(seg.obs, seg.offsets, rng,
                          send_offsets=seg.send_offsets)
        outputs.append(SegmentOutput(
            fit=fit,
            first_host_time=min(o.t0 for o in seg.obs),
            last_host_time=max(o.t3 for o in seg.obs),
        ))
    return outputs, events, segments
