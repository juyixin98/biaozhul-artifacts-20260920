"""Offline analysis: segment observations, explain discontinuities, fit.

The analyzer turns a batch of request/response samples into:

* a list of *segments*, each with its own interval-regression fit
  (:mod:`app.fitting`);
* a list of *events* describing discontinuities and the evidence behind them
  (counter wrap vs device restart vs host/device time step vs drift change);
* an overall ``status``: ``ok`` / ``uncertain`` / ``insufficient_evidence``.

Counter wrap and device restart are distinguished using evidence, never
guessed: a wrap must be explainable by an integer number of modulus turns at
the rough drift rate, a restart must show a counter back near zero, and when
both stories fit the event is reported as ambiguous and the result is
``uncertain``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .config import AnalyzerConfig
from .fitting import FitResult, Observation, fit_segment, rtt_filter


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class Event:
    type: str  # wrap | restart | wrap_or_restart | counter_gap |
    # counter_stall | time_jump | drift_change
    at_sample_index: int  # boundary is between index-1 and index (sorted order)
    detail: dict = field(default_factory=dict)


@dataclass
class Segment:
    segment_id: int
    epoch: int
    sample_indices: list[int]
    counter_start: float  # raw counter value at the first sample of the epoch
    modulus: float | None
    fit: FitResult
    host_valid_from: float
    host_valid_to: float


@dataclass
class AnalysisResult:
    status: str  # ok | uncertain | insufficient_evidence
    reasons: list[str]
    device_id: str
    modulus: float | None
    n_input: int
    n_rtt_dropped: list[int]
    events: list[Event]
    segments: list[Segment]
    recommended_segment_id: int | None
    median_rtt: float
    config: AnalyzerConfig

    # ----- serialisation -----
    def to_dict(self) -> dict:
        out: dict = {
            "status": self.status,
            "reasons": self.reasons,
            "device_id": self.device_id,
            "modulus": self.modulus,
            "n_input": self.n_input,
            "n_rtt_dropped": self.n_rtt_dropped,
            "median_rtt": self.median_rtt,
            "events": [asdict(e) for e in self.events],
            "segments": [_segment_dict(s) for s in self.segments],
            "recommended_segment_id": self.recommended_segment_id,
        }
        return out


def _fit_dict(fit: FitResult) -> dict:
    return {
        "feasible": fit.feasible,
        "n_used": fit.n_used,
        "n_input": fit.n_input,
        "trimmed_sample_indices": list(fit.trimmed_idx),
        "alpha": {
            "point": _num(fit.alpha_point),
            "lower": _num(fit.alpha_lo),
            "upper": _num(fit.alpha_hi),
        },
        "beta": {
            "point": _num(fit.beta_point),
            "lower": _num(fit.beta_lo),
            "upper": _num(fit.beta_hi),
            "relative_halfwidth": _num(fit.drift_halfwidth_rel()),
        },
        "offset_halfwidth_s": _num(fit.offset_halfwidth()),
        "residual_rms_s": _num(fit.residual_rms),
        "notes": fit.reasons,
    }


def _num(x: float) -> float | None:
    return float(x) if np.isfinite(x) else None


def _segment_dict(s: Segment) -> dict:
    d = {
        "segment_id": s.segment_id,
        "epoch": s.epoch,
        "sample_indices": s.sample_indices,
        "counter_start": s.counter_start,
        "modulus": s.modulus,
        "host_valid_from": s.host_valid_from,
        "host_valid_to": s.host_valid_to,
        "fit": _fit_dict(s.fit),
    }
    if s.fit.feasible:
        c0, c1 = s.fit._c[0], s.fit._c[-1]
        p0, l0, h0 = s.fit.predict(c0)
        p1, l1, h1 = s.fit.predict(c1)
        d["counter_range"] = [_num(c0), _num(c1)]
        d["host_at_counter_start"] = {"point": p0, "lower": l0, "upper": h0}
        d["host_at_counter_end"] = {"point": p1, "lower": l1, "upper": h1}
    return d


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze(
    observations: list[Observation],
    *,
    device_id: str,
    modulus: float | None,
    config: AnalyzerConfig | None = None,
) -> AnalysisResult:
    cfg = config or AnalyzerConfig()
    n = len(observations)
    events: list[Event] = []
    reasons: list[str] = []

    order = sorted(range(n), key=lambda i: observations[i].t_send)
    t_send = np.array([observations[i].t_send for i in order], dtype=float)
    t_recv = np.array([observations[i].t_recv for i in order], dtype=float)
    counter = np.array([observations[i].counter for i in order], dtype=float)

    if np.any(t_recv < t_send):
        bad = int(np.argmax(t_recv < t_send))
        raise ValueError(
            f"observation at sorted index {bad}: t_recv < t_send"
        )

    # --- 1. RTT outlier filtering ------------------------------------------
    rf = rtt_filter(
        t_send, t_recv, z_threshold=cfg.rtt_z_threshold, min_keep=2
    )
    rtt = t_recv - t_send
    surv = np.where(rf.keep)[0]
    rtt_dropped = [int(order[i]) for i in np.where(~rf.keep)[0]]
    if len(rtt_dropped):
        events.append(
            Event(
                type="rtt_outlier",
                at_sample_index=-1,
                detail={"dropped_input_indices": rtt_dropped},
            )
        )

    result = AnalysisResult(
        status="insufficient_evidence",
        reasons=reasons,
        device_id=device_id,
        modulus=modulus,
        n_input=n,
        n_rtt_dropped=rtt_dropped,
        events=events,
        segments=[],
        recommended_segment_id=None,
        median_rtt=rf.median_rtt,
        config=cfg,
    )

    if len(surv) < cfg.min_segment_points:
        reasons.append(
            f"only {len(surv)} usable sample(s) after RTT filtering; "
            f"need at least {cfg.min_segment_points}"
        )
        return result

    ts = t_send[surv]
    tr = t_recv[surv]
    cc = counter[surv]
    rt = rtt[surv]
    mid = 0.5 * (ts + tr)

    # --- 2. rough drift rate from monotone adjacent counter moves ----------
    dmid = np.diff(mid)
    dc = np.diff(cc)
    pos = dc > 0
    beta_rough = (
        float(np.median(dmid[pos] / dc[pos])) if np.any(pos) else float("nan")
    )
    if not np.isfinite(beta_rough) or beta_rough <= 0:
        reasons.append("counter never advances: drift is not identifiable")
        return result

    # robust scale of adjacent-duration residuals for wrap consistency tests
    gap_resid = dmid[pos] - beta_rough * dc[pos]
    gap_med = float(np.median(gap_resid))
    gap_mad = 1.4826 * float(np.median(np.abs(gap_resid - gap_med)))
    wrap_tol = max(
        cfg.wrap_time_tol_mad * gap_mad,
        cfg.wrap_time_tol_rtt * rf.median_rtt,
    )

    # --- 3. scan boundaries; build unwrapped epochs ------------------------
    # epoch membership per surviving sample; unwrapped counter per sample
    epoch = np.zeros(len(surv), dtype=int)
    unwrap = np.zeros(len(surv), dtype=float)
    epoch_start_raw = [float(cc[0])]
    boundary_split: set[int] = set()  # local survivor indices of segment cuts
    cur_epoch = 0
    base = 0.0  # unwrapped value contribution at sample 0
    unwrap[0] = cc[0] - epoch_start_raw[0]
    for i in range(1, len(surv)):
        delta_raw = cc[i] - cc[i - 1]
        host_gap = mid[i] - mid[i - 1]
        if delta_raw < 0:
            # candidate: wrap? restart? ambiguous?
            wrap_k = None
            if modulus is not None and modulus > 0:
                best = None
                for k in range(1, cfg.wrap_max_turns + 1):
                    exp = beta_rough * (delta_raw + k * modulus)
                    err = abs(host_gap - exp)
                    if err <= wrap_tol and (best is None or err < best[1]):
                        best = (k, err)
                if best is not None:
                    wrap_k = best[0]
            near_zero = (
                modulus is not None
                and modulus > 0
                and cc[i] <= cfg.restart_zero_fraction * modulus
            )
            # A wrap leaves the counter just past zero by construction; being
            # "near zero" is therefore NOT, by itself, evidence of a restart.
            # Ambiguity is only real when BOTH stories fit the timing: wrap
            # consistent with the gap AND the counter is within ~2 % of zero
            # with essentially no elapsed time (an immediate reboot).
            immediate_reboot = (
                near_zero
                and cc[i] <= 0.02 * (modulus or 1.0)
                and host_gap <= max(wrap_tol, 4.0 * rf.median_rtt)
            )
            if wrap_k is not None and not immediate_reboot:
                base += wrap_k * modulus  # unwrap must rise by kM + delta_raw
                events.append(
                    Event(
                        type="wrap",
                        at_sample_index=int(surv[i]),
                        detail={
                            "turns": wrap_k,
                            "time_residual_s": float(
                                host_gap
                                - beta_rough
                                * (delta_raw + wrap_k * (modulus or 0.0))
                            ),
                            "tolerance_s": float(wrap_tol),
                        },
                    )
                )
            elif wrap_k is None and near_zero:
                cur_epoch += 1
                epoch_start_raw.append(float(cc[i]))
                base = 0.0
                boundary_split.add(i)
                events.append(
                    Event(
                        type="restart",
                        at_sample_index=int(surv[i]),
                        detail={
                            "counter_before": float(cc[i - 1]),
                            "counter_after": float(cc[i]),
                        },
                    )
                )
            elif wrap_k is not None and immediate_reboot:
                cur_epoch += 1
                epoch_start_raw.append(float(cc[i]))
                base = 0.0
                boundary_split.add(i)
                events.append(
                    Event(
                        type="wrap_or_restart",
                        at_sample_index=int(surv[i]),
                        detail={
                            "wrap_turns_plausible": wrap_k,
                            "counter_after_fraction_of_modulus": float(
                                cc[i] / (modulus or 1.0)
                            ),
                            "host_gap_s": float(host_gap),
                        },
                    )
                )
            elif modulus is None:
                cur_epoch += 1
                epoch_start_raw.append(float(cc[i]))
                base = 0.0
                boundary_split.add(i)
                events.append(
                    Event(
                        type="wrap_or_restart",
                        at_sample_index=int(surv[i]),
                        detail={
                            "counter_before": float(cc[i - 1]),
                            "counter_after": float(cc[i]),
                            "note": "counter moved backwards with no modulus "
                            "information; wrap and restart are both possible",
                        },
                    )
                )
            else:
                # negative move with no modulus information / no matching k
                cur_epoch += 1
                epoch_start_raw.append(float(cc[i]))
                base = 0.0
                boundary_split.add(i)
                events.append(
                    Event(
                        type="restart",
                        at_sample_index=int(surv[i]),
                        detail={
                            "counter_before": float(cc[i - 1]),
                            "counter_after": float(cc[i]),
                            "note": "counter moved backwards; no consistent "
                            "wrap hypothesis available",
                        },
                    )
                )
        elif delta_raw == 0:
            # counter reported the same value: stalled tick -- still the same
            # epoch, but a stall may bracket a stopped clock; mark a segment
            # boundary so the fit does not bridge it blindly.
            if host_gap > wrap_tol:
                boundary_split.add(i)
                events.append(
                    Event(
                        type="counter_stall",
                        at_sample_index=int(surv[i]),
                        detail={"host_gap_s": float(host_gap)},
                    )
                )
        else:
            # monotone positive move: a huge unexplained gap means missing
            # ticks (or an unreported wrap).  Compare against the *local*
            # drift rate so a genuine drift change elsewhere does not flag
            # every later sample as a gap.
            lo_w = max(0, i - 6)
            local = dmid[lo_w:i][dc[lo_w:i] > 0] / dc[lo_w:i][dc[lo_w:i] > 0]
            beta_local = (
                float(np.median(local)) if len(local) else beta_rough
            )
            err = abs(host_gap - beta_local * delta_raw)
            if err > wrap_tol and beta_local * delta_raw > 0:
                events.append(
                    Event(
                        type="counter_gap",
                        at_sample_index=int(surv[i]),
                        detail={
                            "counter_delta": float(delta_raw),
                            "host_gap_s": float(host_gap),
                            "expected_gap_s": float(beta_local * delta_raw),
                        },
                    )
                )
        epoch[i] = cur_epoch
        unwrap[i] = base + cc[i] - epoch_start_raw[cur_epoch]

    # --- 4. changepoint detection inside each epoch ------------------------
    final_pieces: list[tuple[int, np.ndarray]] = []
    for ep in range(cur_epoch + 1):
        idx_local = np.where(epoch == ep)[0]
        forced = sorted(i for i in boundary_split if epoch[i] == ep)
        cuts = [idx_local[0]] + forced + [idx_local[-1] + 1]
        for a, b in zip(cuts[:-1], cuts[1:]):
            piece = idx_local[(idx_local >= a) & (idx_local < b)]
            if len(piece) == 0:
                continue
            final_pieces.append(
                (ep, _detect_changepoints(piece, ts, tr, unwrap, rt, events,
                                          surv, cfg, depth=0))
            )
    # flatten recursively returned piece arrays
    pieces: list[tuple[int, np.ndarray]] = []

    def _flatten(ep, p):
        if isinstance(p, list):
            for q in p:
                _flatten(ep, q)
        else:
            pieces.append((ep, p))

    for ep, p in final_pieces:
        _flatten(ep, p)

    # --- 5. fit every piece -------------------------------------------------
    segments: list[Segment] = []
    for sid, (ep, piece) in enumerate(pieces):
        fr = fit_segment(
            unwrap[piece], ts[piece], tr[piece], rtt=rt[piece],
            min_points=max(3, cfg.min_segment_points - 1),
        )
        segments.append(
            Segment(
                segment_id=sid,
                epoch=ep,
                sample_indices=[int(surv[i]) for i in piece],
                counter_start=epoch_start_raw[ep],
                modulus=modulus,
                fit=fr,
                host_valid_from=float(ts[piece][0]),
                host_valid_to=float(tr[piece][-1]),
            )
        )

    result.segments = segments

    # --- 6. recommend / status ---------------------------------------------
    feasible = [s for s in segments if s.fit.feasible]
    if not feasible:
        reasons.append("no segment produced a feasible fit")
        return result
    # most recent feasible segment is the calibration to publish
    rec = max(feasible, key=lambda s: s.host_valid_to)
    result.recommended_segment_id = rec.segment_id

    if rec.fit.n_used < cfg.min_segment_points:
        reasons.append(
            f"recommended segment has only {rec.fit.n_used} samples; "
            f"need {cfg.min_segment_points}"
        )
        result.status = "insufficient_evidence"
        return result

    # counter_gap events adjacent to a confirmed time/drift changepoint are
    # explained by that event rather than missing evidence; clear a window
    # wide enough to cover both neighbours of the kink
    explained: set[int] = set()
    for e in events:
        if e.type in {"time_jump", "drift_change"}:
            for q in range(-4, 5):
                explained.add(e.at_sample_index + q)
    events[:] = [
        e
        for e in events
        if not (e.type == "counter_gap" and e.at_sample_index in explained)
    ]

    ambiguous = {
        e.type for e in events if e.type in {"wrap_or_restart", "counter_gap"}
    }
    status = "ok"
    hw_off = rec.fit.offset_halfwidth()
    hw_rel = rec.fit.drift_halfwidth_rel()
    if hw_off > cfg.offset_uncertainty_max_s:
        reasons.append(
            f"offset half-width {hw_off*1e3:.2f} ms exceeds "
            f"{cfg.offset_uncertainty_max_s*1e3:.0f} ms"
        )
        status = "uncertain"
    if hw_rel > cfg.drift_uncertainty_max_rel:
        reasons.append(
            f"drift relative half-width {hw_rel*100:.3f}% exceeds "
            f"{cfg.drift_uncertainty_max_rel*100:.3f}%"
        )
        status = "uncertain"
    if ambiguous:
        reasons.append(
            "ambiguous event(s) with insufficient evidence: "
            + ", ".join(sorted(ambiguous))
        )
        status = "uncertain"
    if not rec.fit.beta_lo > 0:
        reasons.append("drift rate interval includes non-positive values")
        status = "uncertain"
    result.status = status
    return result


# ---------------------------------------------------------------------------
# Changepoint detection (time step / drift change) within one epoch piece
# ---------------------------------------------------------------------------


def _midline(x: np.ndarray, mid: np.ndarray) -> tuple[float, float]:
    """Robust Theil-Sen line through interval midpoints."""

    from .fitting import _theil_sen_beta

    b = _theil_sen_beta(x, mid)
    a = float(np.median(mid - b * x))
    return a, b


def _detect_changepoints(
    piece: np.ndarray,
    ts: np.ndarray,
    tr: np.ndarray,
    unwrap: np.ndarray,
    rt: np.ndarray,
    events: list[Event],
    surv: np.ndarray,
    cfg: AnalyzerConfig,
    depth: int,
):
    """Recursively split a piece at its strongest supported changepoint.

    Works even when the whole piece is interval-infeasible (that is exactly
    when a step must be found): residuals come from a robust Theil-Sen midpoint
    line, and the noise scale is estimated from *first differences*, which a
    single jump barely inflates.
    """

    min_side = cfg.min_changepoint_side
    if len(piece) < 2 * min_side:
        return piece

    x = unwrap[piece]
    mid = 0.5 * (ts[piece] + tr[piece])

    # Noise scale: detrend each half independently with its own Theil-Sen line
    # so a genuine drift change does not inflate the scale with its ramp.  The
    # first differences of the detrended residuals expose a jump cleanly.
    half = len(piece) // 2
    def _detrend(sl):
        if len(x[sl]) >= 2 and np.ptp(x[sl]) > 0:
            ah, bh = _midline(x[sl], mid[sl])
            return mid[sl] - (ah + bh * x[sl])
        return mid[sl]
    detr = np.concatenate([_detrend(slice(0, half)),
                           _detrend(slice(half, None))])
    dres = np.diff(detr)
    step_scale = 1.042 * float(np.median(np.abs(dres - np.median(dres))))
    med_rtt = float(np.median(rt[piece]))
    step_threshold = max(6.0 * step_scale, 10.0 * med_rtt)

    # Scan every admissible cut and keep the single globally strongest event.
    # Drift change requires disjoint beta intervals and a substantial rate
    # ratio; a mere accumulated ramp (step without disjoint rates) is a jump.
    best = None  # (score, kind, k, payload)
    for k in range(min_side, len(piece) - min_side + 1):
        L, R = piece[:k], piece[k:]

        fL = fit_segment(
            unwrap[L], ts[L], tr[L],
            min_points=max(3, min_side - 1),
        )
        fR = fit_segment(
            unwrap[R], ts[R], tr[R],
            min_points=max(3, min_side - 1),
        )
        # Horizontal break: gap in host time between the two lines evaluated
        # at a *common counter* x_break.  A pure clock jump maps one counter
        # value to two host times, so this gap equals the jump even though
        # the slopes agree; a pure drift change is continuous at the kink and
        # gives a gap ~0 (only the slopes differ).  This is the discriminator
        # that stops a drift ramp masquerading as a series of jumps.
        aL, bL = _midline(unwrap[L], 0.5 * (ts[L] + tr[L]))
        aR, bR = _midline(unwrap[R], 0.5 * (ts[R] + tr[R]))
        x_break = unwrap[piece[k]]
        step = float((aR + bR * x_break) - (aL + bL * x_break))

        # Robust rate change from Theil-Sen slopes; corroborate with the
        # hard interval-regression separation when the data are tight.
        midslope = max(bL, bR) / max(min(bL, bR), 1e-30)
        drift_gap = 0.0
        if (
            fL.feasible
            and fR.feasible
            and (fL.beta_hi < fR.beta_lo or fR.beta_hi < fL.beta_lo)
        ):
            if fL.beta_hi < fR.beta_lo:
                gap = fR.beta_lo - fL.beta_hi
            else:
                gap = fL.beta_lo - fR.beta_hi
            bscale = max(abs(fL.beta_point), abs(fR.beta_point), 1e-30)
            drift_gap = gap / bscale

        step_score = abs(step) / max(step_threshold, 1e-12)

        # A genuine drift change needs well-populated, interval-feasible sides
        # with substantially different robust slopes, and the two lines must
        # meet near the break (vertical gap ~ 0).  Interval separation is
        # corroborating evidence, not a hard requirement (finite samples give
        # overlapping beta intervals even under a real rate change).
        enough_sides = (
            len(L) >= cfg.min_drift_change_side
            and len(R) >= cfg.min_drift_change_side
        )
        if (
            enough_sides
            and fL.feasible
            and fR.feasible
            and midslope >= 1.25
            and abs(step) <= max(step_threshold, 0.25)
        ):
            score = 100.0 * midslope + drift_gap  # decisive rate change wins
            rL = fL.beta_point if fL.feasible else bL
            rR = fR.beta_point if fR.feasible else bR
            kind = "drift_change"
            payload = {
                "beta_before": float(rL),
                "beta_after": float(rR),
                "beta_ratio": float(midslope),
                "beta_interval_gap_rel": float(drift_gap),
                "vertical_gap_at_break_s": float(step),
            }
        elif step_score > 1.0:
            score = step_score
            kind = "time_jump"
            payload = {
                "step_s": step,
                "step_noise_scale_s": float(step_scale),
                "median_rtt_s": med_rtt,
                "attribution": "indeterminate",
            }
        else:
            continue
        if best is None or score > best[0]:
            best = (score, kind, k, payload)

    if best is None:
        return piece

    _, kind, k, payload = best
    events.append(
        Event(type=kind, at_sample_index=int(surv[piece[k]]), detail=payload)
    )
    L, R = piece[:k], piece[k:]
    # After a drift change the two sides have deliberately different slopes;
    # re-scanning each side would turn the slope ramp into phantom jumps, so
    # only recurse past actual time jumps (which may themselves be multiple).
    if kind == "drift_change":
        return [L, R]
    if depth + 1 >= cfg.max_changepoint_depth:
        return [L, R]
    return [
        _detect_changepoints(
            L, ts, tr, unwrap, rt, events, surv, cfg, depth + 1
        ),
        _detect_changepoints(
            R, ts, tr, unwrap, rt, events, surv, cfg, depth + 1
        ),
    ]
