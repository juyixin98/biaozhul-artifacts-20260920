"""Core clock model fitting: map a device counter onto host wall-clock.

Every request/response sample constrains the *true* device reading time to an
interval::

    lo_i = t_send_i        (host clock: request left the host)
    hi_i = t_recv_i        (host clock: response came back)

and the device counter is assumed linear in host time *inside one segment*::

    h = alpha + beta * c        (c = counter, unwrapped within the segment)

No symmetry assumption is made about the one-way delays.  Each sample only
yields the interval constraint ``lo_i <= alpha + beta*c_i <= hi_i``.  From all
pairs we derive hard bounds on ``beta``, hull-style envelopes on ``alpha`` and
hence an honest uncertainty interval on any predicted host time.  Constant
asymmetric delay is absorbed into ``alpha`` uncertainty; it never silently
biases ``beta``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# Input containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One request/response round trip."""

    t_send: float  # host time (s) when the request left
    t_recv: float  # host time (s) when the response arrived
    counter: float  # device counter reported inside the response

    @property
    def rtt(self) -> float:
        return self.t_recv - self.t_send


@dataclass(frozen=True)
class RttFilterResult:
    keep: np.ndarray  # boolean mask
    dropped_idx: np.ndarray
    median_rtt: float
    scale: float


# ---------------------------------------------------------------------------
# Robust RTT filtering
# ---------------------------------------------------------------------------


def rtt_filter(
    t_send: np.ndarray,
    t_recv: np.ndarray,
    *,
    z_threshold: float = 3.5,
    min_keep: int = 4,
) -> RttFilterResult:
    """Drop round trips whose RTT is a robust outlier.

    Uses median + MAD (Rousseeuw & Croux style, scale 1.4826).  When the MAD is
    zero we fall back to the standard deviation; if that is also zero every
    sample is kept (nothing identifies an outlier).  ``min_keep`` guarantees we
    never thin a small batch below the minimum needed to fit.
    """

    rtt = np.asarray(t_recv, dtype=float) - np.asarray(t_send, dtype=float)
    med = float(np.median(rtt))
    # Robust upper-tail scale.  Plain MAD over the whole batch is inflated by
    # the spikes we are trying to find; use the upper semi-interquartile range
    # (Q3-Q2), which measures the congestion-free bulk's upward jitter and is
    # barely affected by a handful of extreme values.  With exponential
    # |N(0,s)| jitter this ~ s, so the z threshold stays interpretable.
    q2, q3 = np.quantile(rtt, [0.5, 0.75])
    scale = float(q3 - q2)
    if scale == 0.0:
        mad = float(np.median(np.abs(rtt - med)))
        scale = 1.4826 * mad

    if scale == 0.0:
        keep = np.ones(rtt.shape, dtype=bool)
    else:
        # Anomalous round trips for clock fitting are *long* (congestion,
        # scheduling); an unusually short RTT carries useful tight timing and
        # must not be rejected, so the test is deliberately one-sided.
        raw_keep = (rtt - med) <= z_threshold * scale
        n_violators = int((~raw_keep).sum())
        if len(rtt) - n_violators < min_keep and n_violators > 0:
            # more violators than we can afford to drop: pardon the least-bad
            # ones until min_keep samples remain; never drop a clean sample
            violator_idx = np.where(~raw_keep)[0]
            badness = np.abs(rtt[violator_idx] - med)
            pardon = violator_idx[np.argsort(badness)][: min_keep - (len(rtt) - n_violators)]
            keep = raw_keep.copy()
            keep[pardon] = True
        else:
            keep = raw_keep
    return RttFilterResult(
        keep=keep,
        dropped_idx=np.where(~keep)[0],
        median_rtt=med,
        scale=scale,
    )


# ---------------------------------------------------------------------------
# Fit result
# ---------------------------------------------------------------------------


@dataclass
class FitResult:
    n_input: int
    n_used: int
    feasible: bool
    beta_point: float = float("nan")
    beta_lo: float = float("nan")
    beta_hi: float = float("nan")
    alpha_point: float = float("nan")
    alpha_lo: float = float("nan")  # at beta_point
    alpha_hi: float = float("nan")  # at beta_point
    residual_rms: float = float("nan")
    trimmed_idx: tuple[int, ...] = field(default_factory=tuple)
    reasons: list[str] = field(default_factory=list)

    # counters / bounds retained from the fit (unwrapped segment coordinates)
    _c: np.ndarray | None = None
    _lo: np.ndarray | None = None
    _hi: np.ndarray | None = None

    def predict(self, counter: float) -> tuple[float, float, float]:
        """Return (point estimate, lower bound, upper bound) of host time.

        Bounds are the extrema of every (alpha, beta) pair consistent with the
        interval constraints and the fitted beta range -- see module docstring.
        """

        if not self.feasible:
            return float("nan"), float("nan"), float("nan")
        point = self.alpha_point + self.beta_point * counter
        lo, hi = prediction_bounds(
            counter,
            self._c,
            self._lo,
            self._hi,
            self.beta_lo,
            self.beta_hi,
        )
        return point, lo, hi

    def offset_halfwidth(self) -> float:
        if not self.feasible:
            return float("inf")
        return 0.5 * (self.alpha_hi - self.alpha_lo)

    def drift_halfwidth_rel(self) -> float:
        if not self.feasible or self.beta_point == 0.0:
            return float("inf")
        return 0.5 * (self.beta_hi - self.beta_lo) / abs(self.beta_point)


# ---------------------------------------------------------------------------
# Pairwise beta bounds
# ---------------------------------------------------------------------------


def _pair_beta_bounds(c: np.ndarray, lo: np.ndarray, hi: np.ndarray):
    """Per-pair allowable beta intervals.

    For pair i,j with c_i > c_j = d::

        (lo_i - hi_j)/d <= beta <= (hi_i - lo_j)/d
    """

    n = len(c)
    ii, jj = np.triu_indices(n, k=1)
    d = c[ii] - c[jj]
    m = d != 0.0
    ii, jj, d = ii[m], jj[m], d[m]
    pos = d > 0
    # orient so c_hi > c_lo
    c_hi = np.where(pos, ii, jj)
    c_lo = np.where(pos, jj, ii)
    d = np.abs(d)
    blo = (lo[c_hi] - hi[c_lo]) / d
    bhi = (hi[c_hi] - lo[c_lo]) / d
    return ii, jj, blo, bhi


def _beta_range(
    c: np.ndarray, lo: np.ndarray, hi: np.ndarray
) -> tuple[float, float, bool]:
    """Intersect all per-pair beta intervals.

    Also enforces that equal-counter samples have overlapping host intervals.
    Returns (beta_lo, beta_hi, feasible).
    """

    n = len(c)
    # equal-counter overlap check
    order = np.argsort(c, kind="mergesort")
    cs = c[order]
    los, his = lo[order], hi[order]
    grp_start = 0
    for k in range(1, n + 1):
        if k == n or cs[k] != cs[grp_start]:
            if k - grp_start > 1:
                if np.max(los[grp_start:k]) > np.min(his[grp_start:k]):
                    return 0.0, 0.0, False
            grp_start = k

    _, _, blo, bhi = _pair_beta_bounds(c, lo, hi)
    beta_lo = float(np.max(blo))
    beta_hi = float(np.min(bhi))
    if not np.isfinite(beta_lo) or not np.isfinite(beta_hi):
        return beta_lo, beta_hi, False
    return beta_lo, beta_hi, beta_lo <= beta_hi


# ---------------------------------------------------------------------------
# Theil-Sen point slope (median pair slope of interval midpoints)
# ---------------------------------------------------------------------------


def _theil_sen_beta(c: np.ndarray, mid: np.ndarray) -> float:
    ii, jj = np.triu_indices(len(c), k=1)
    d = c[ii] - c[jj]
    m = d != 0.0
    slopes = (mid[ii[m]] - mid[jj[m]]) / d[m]
    return float(np.median(slopes))


# ---------------------------------------------------------------------------
# Prediction envelope
# ---------------------------------------------------------------------------


def prediction_bounds(
    x: float,
    c: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    beta_lo: float,
    beta_hi: float,
) -> tuple[float, float]:
    """Hard host-time interval at counter ``x``.

    ``alpha_lo(beta) = max_i(lo_i - beta*c_i)`` and
    ``alpha_hi(beta) = min_i(hi_i - beta*c_i)`` are piecewise-linear hull
    envelopes.  Their extrema over [beta_lo, beta_hi] occur at interval ends or
    at pairwise line intersections (envelope kinks), so those candidate betas
    are enumerated exactly.
    """

    n = len(c)
    ii, jj = np.triu_indices(n, k=1)
    d = c[ii] - c[jj]
    m = d != 0.0
    ii, jj, d = ii[m], jj[m], d[m]

    # intersections of (a_i - beta*c_i) with (a_j - beta*c_j) for both lo/hi
    cand = [beta_lo, beta_hi]
    with np.errstate(divide="ignore", invalid="ignore"):
        x1 = (lo[ii] - lo[jj]) / d
        x2 = (hi[ii] - hi[jj]) / d
    for arr in (x1, x2):
        sel = arr[np.isfinite(arr) & (arr >= beta_lo) & (arr <= beta_hi)]
        cand.append(float(np.min(sel)) if len(sel) else beta_lo)
        cand.append(float(np.max(sel)) if len(sel) else beta_hi)
    # sample midpoints too (cheap safety net)
    grid = np.linspace(beta_lo, beta_hi, 65)
    betas = np.unique(np.concatenate([np.asarray(cand, dtype=float), grid]))

    # g_lo(beta) = beta*x + max_i(lo_i - beta*c_i)  -> global inf
    # g_hi(beta) = beta*x + min_i(hi_i - beta*c_i)  -> global sup
    # vectorise over candidate betas in chunks
    lows, highs = [], []
    for chunk in np.array_split(betas, max(1, len(betas) // 256 + 1)):
        B = chunk[:, None]
        base = B * x
        lows.append(base + np.max(lo[None, :] - B * c[None, :], axis=1))
        highs.append(base + np.min(hi[None, :] - B * c[None, :], axis=1))
    return float(np.min(np.concatenate(lows))), float(
        np.max(np.concatenate(highs))
    )


# ---------------------------------------------------------------------------
# Main fit
# ---------------------------------------------------------------------------


def fit_segment(
    counters: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    *,
    rtt: np.ndarray | None = None,
    min_points: int = 6,
    max_trim_fraction: float = 0.35,
) -> FitResult:
    """Fit ``h = alpha + beta*c`` under interval uncertainty.

    ``counters`` must already be unwrapped (monotone non-decreasing within the
    segment).  Inconsistent intervals (caused by an undetected jump inside the
    segment) are repaired by trimming the worst-offending endpoint, bounded by
    ``max_trim_fraction``; beyond that the fit is reported infeasible rather
    than forcing an answer.
    """

    c = np.asarray(counters, dtype=float)
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    n0 = len(c)
    res = FitResult(n_input=n0, n_used=n0, feasible=False)

    if n0 < 2 or np.all(c == c[0]):
        res.reasons.append("no counter span: drift is not identifiable")
        return res

    alive = np.ones(n0, dtype=bool)
    trimmed: list[int] = []

    def current():
        idx = np.where(alive)[0]
        return idx, c[idx], lo[idx], hi[idx]

    beta_lo = beta_hi = 0.0
    feasible = False
    while True:
        idx, cc, lc, hc = current()
        beta_lo, beta_hi, feasible = _beta_range(cc, lc, hc)
        if feasible:
            break
        if len(idx) <= max(2, int(round(min_points * 0.67))) or (
            len(trimmed) >= max(1, int(np.floor(max_trim_fraction * n0)))
        ):
            res.trimmed_idx = tuple(trimmed)
            res.n_used = int(alive.sum())
            res.reasons.append(
                "interval constraints inconsistent after trimming; "
                "possible unmodelled jump inside segment"
            )
            return res
        # find the most violated pair and drop its worse endpoint
        ii, jj, blo, bhi = _pair_beta_bounds(cc, lc, hc)
        viol = np.maximum(blo - beta_hi, beta_lo - bhi)
        k = int(np.argmax(viol))
        a, b = int(ii[k]), int(jj[k])
        ga, gb = int(idx[a]), int(idx[b])
        if rtt is not None:
            drop = ga if rtt[ga] >= rtt[gb] else gb
        else:
            # fall back: endpoint farther from midpoint line
            mid = 0.5 * (lc + hc)
            ts = _theil_sen_beta(cc, mid)
            inter = np.median(mid - ts * cc)
            ra = abs(mid[a] - (inter + ts * cc[a]))
            rb = abs(mid[b] - (inter + ts * cc[b]))
            drop = ga if ra >= rb else gb
        alive[drop] = False
        trimmed.append(drop)

    # The interval system is feasible, but a single gross outlier (e.g. one
    # wildly delayed response) merely *widens* one interval without violating
    # any pair constraint; it would otherwise inflate the reported envelope.
    # Detect such points by midpoint residual against the robust line and
    # re-enter the feasibility loop if any are removed.
    def _midpoint_outlier():
        idx, cc, lc, hc = current()
        if len(idx) <= max(4, int(round(min_points * 0.8))):
            return None
        mm = 0.5 * (lc + hc)
        b = _theil_sen_beta(cc, mm)
        a = float(np.median(mm - b * cc))
        rr = mm - (a + b * cc)
        med = float(np.median(rr))
        mad = 1.4826 * float(np.median(np.abs(rr - med)))
        if mad <= 0.0:
            return None
        if len(trimmed) >= max(1, int(np.floor(max_trim_fraction * n0))):
            return None
        z = np.abs(rr - med) / mad
        k = int(np.argmax(z))
        if z[k] >= 25.0:  # gross outlier only; normal jitter stays
            return int(idx[k])
        return None

    while feasible:
        drop = _midpoint_outlier()
        if drop is None:
            break
        alive[drop] = False
        trimmed.append(drop)
        idx, cc, lc, hc = current()
        beta_lo, beta_hi, feasible = _beta_range(cc, lc, hc)
        if not feasible:
            # restore and stop: removing it breaks feasibility, so keep it
            alive[drop] = True
            trimmed.pop()
            feasible = True
            break

    idx, cc, lc, hc = current()
    mid = 0.5 * (lc + hc)
    beta_star = np.clip(_theil_sen_beta(cc, mid), beta_lo, beta_hi)
    alpha_lo = float(np.max(lc - beta_star * cc))
    alpha_hi = float(np.min(hc - beta_star * cc))
    alpha_star = 0.5 * (alpha_lo + alpha_hi)
    resid = mid - (alpha_star + beta_star * cc)

    res.feasible = True
    res.n_used = len(cc)
    res.trimmed_idx = tuple(sorted(trimmed))
    res.beta_point, res.beta_lo, res.beta_hi = (
        float(beta_star),
        float(beta_lo),
        float(beta_hi),
    )
    res.alpha_point, res.alpha_lo, res.alpha_hi = (
        float(alpha_star),
        alpha_lo,
        alpha_hi,
    )
    res.residual_rms = float(np.sqrt(np.mean(resid**2)))
    res._c, res._lo, res._hi = cc, lc, hc
    if len(trimmed) > 0:
        res.reasons.append(
            f"trimmed {len(trimmed)} sample(s) with inconsistent intervals"
        )
    return res
