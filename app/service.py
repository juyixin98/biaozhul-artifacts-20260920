"""Orchestration: calibration runs, signed publishing, versioned conversion."""

from __future__ import annotations

import json
import math
from typing import Any, Optional

import numpy as np

from . import crypto
from .fitting import (
    Observation,
    calibrate,
    predict_interval,
)
from .schemas import (
    CalibrationResponse,
    ConvertResponse,
    Discontinuity,
    Interval,
    ModelSummary,
    SegmentReport,
)
from .storage import Store


def _segment_report(index: int, seg_out) -> SegmentReport:
    f = seg_out.fit
    slope_ci = Interval(lower=f.slope_ci[0], upper=f.slope_ci[1]) if f.slope_ci else None
    intercept_ci = (
        Interval(lower=f.intercept_ci[0], upper=f.intercept_ci[1])
        if f.intercept_ci else None
    )
    return SegmentReport(
        index=index,
        status=f.status,
        n_samples_input=f.n_input,
        n_samples_used=f.n_used,
        n_samples_rtt_filtered=f.n_rtt_filtered,
        host_time_range=(seg_out.first_host_time, seg_out.last_host_time),
        counter_range_unwrapped=(
            (f.c_min, f.c_max) if f.c_min is not None else (0.0, 0.0)
        ),
        slope=f.slope,
        intercept=f.intercept,
        slope_ci=slope_ci,
        intercept_ci=intercept_ci,
        rtt_median=f.rtt_median,
        rtt_filter_threshold=(None if math.isinf(f.rtt_threshold or math.inf)
                              else f.rtt_threshold),
        reason=f.reason,
    )


class Service:
    def __init__(self, store: Store, key: bytes,
                 rng: Optional[np.random.Generator] = None):
        self.store = store
        self.key = key
        self.rng = rng or np.random.default_rng()

    # ------------------------------------------------------------------
    # Calibration + publish
    # ------------------------------------------------------------------

    def run_calibration(self, device_id: str, modulus: Optional[float],
                        nominal_hz: Optional[float],
                        observations: list[Observation],
                        publish: bool, ingest_id: str) -> CalibrationResponse:
        seg_outputs, events, segments = calibrate(
            observations, modulus, self.rng, nominal_hz=nominal_hz)

        reports = [_segment_report(i, so) for i, so in enumerate(seg_outputs)]
        if nominal_hz is not None:
            for rep, so in zip(reports, seg_outputs):
                if rep.slope is not None:
                    c_lo, c_hi = rep.counter_range_unwrapped
                    c_mid = 0.5 * (c_lo + c_hi)
                    point, lo, hi = predict_interval(so.fit, c_mid)
                    rep.offset_seconds = point - c_mid / nominal_hz
                    rep.offset_interval = Interval(
                        lower=lo - c_mid / nominal_hz,
                        upper=hi - c_mid / nominal_hz,
                    )
                    nominal_slope = 1.0 / nominal_hz
                    # Fitted slope = host-seconds per counter tick, so the
                    # device's actual tick rate is 1/slope.
                    actual_rate = 1.0 / rep.slope
                    rep.drift_ppm = (actual_rate - nominal_hz) / nominal_hz * 1e6
                    if rep.slope_ci is not None and rep.slope_ci.lower > 0:
                        # rate = 1/slope is decreasing in slope -> swap ends
                        rate_lo = 1.0 / rep.slope_ci.upper
                        rate_hi = 1.0 / rep.slope_ci.lower
                        rep.drift_ppm_ci = Interval(
                            lower=(rate_lo - nominal_hz) / nominal_hz * 1e6,
                            upper=(rate_hi - nominal_hz) / nominal_hz * 1e6,
                        )

        disc = [
            Discontinuity(
                type=e.type,
                at_host_time=e.at_host_time,
                evidence=e.evidence,
                from_segment=e.from_segment,
                to_segment=e.to_segment,
            )
            for e in events
        ]

        # Overall status: uncertain unless the LATEST regime is calibrated —
        # only the latest regime can back a forward-looking model.
        latest = reports[-1]
        overall_status = latest.status
        reason = latest.reason if overall_status == "uncertain" else None
        if overall_status == "uncertain" and any(e.type == "uncertain" for e in events):
            reason = (reason or "") + (
                " | an observed discontinuity could not be classified"
            )

        resp = CalibrationResponse(
            device_id=device_id,
            version=None,
            counter_modulus=modulus,
            counter_nominal_hz=nominal_hz,
            status=overall_status,
            reason=reason.strip(" |") if reason else None,
            segments=reports,
            discontinuities=disc,
            published=False,
        )

        self.store.upsert_device(device_id, modulus, nominal_hz)
        rows = [(o.t0, o.t3, o.c_recv, o.c_send, o.seq) for o in observations]
        version_used = self.store.active_version(device_id)
        self.store.add_observations(device_id, ingest_id, version_used, rows)

        if publish and overall_status == "calibrated":
            self._publish(device_id, modulus, nominal_hz,
                          seg_outputs[-1], events, resp)
        elif publish:
            # Refuse silently-unsafe behaviour: caller asked to publish but
            # evidence does not support it.
            resp.published = False
        return resp

    def _next_version(self) -> str:
        # Versions are globally unique (the models table keys on version);
        # zero-padded so lexicographic and chronological order coincide.
        n = len(self.store.list_models(None))
        return f"v{n + 1:06d}"

    def _publish(self, device_id: str, modulus: Optional[float],
                 nominal_hz: Optional[float], latest_seg, events,
                 resp: CalibrationResponse) -> None:
        f = latest_seg.fit
        version = self._next_version()
        valid_from = latest_seg.first_host_time
        # Validity of the newest model starts at its first evidence point;
        # the store closes the previous open model exactly there.
        c0 = 0.5 * (f.c_min + f.c_max)
        point, lo, hi = predict_interval(f, c0)
        payload = {
            "version": version,
            "device_id": device_id,
            "status": "calibrated",
            "counter_modulus": modulus,
            "counter_nominal_hz": nominal_hz,
            "slope": f.slope,
            "intercept": f.intercept,
            "slope_ci": list(f.slope_ci),
            "intercept_ci": list(f.intercept_ci),
            "fit_counter_min": f.c_min,
            "fit_counter_max": f.c_max,
            "fit_host_min": f.t_min,
            "fit_host_max": f.t_max,
            "offset_interval_at_mid": [
                lo - (c0 / nominal_hz if nominal_hz else 0.0),
                hi - (c0 / nominal_hz if nominal_hz else 0.0),
            ],
            "rtt_median": f.rtt_median,
            "n_used": f.n_used,
            "valid_from_host_time": valid_from,
            "wrap_count_at_min": self._wrap_count(modulus, f.c_min),
            "bootstrap": f.boot.tolist() if f.boot is not None else [],
            "envelope_points": [
                {"c": p.c, "lo": _json_float(p.lo), "hi": _json_float(p.hi)}
                for p in (f.points or [])
            ],
        }
        sig = crypto.sign(payload, self.key)
        self.store.publish_model(version, device_id, "calibrated",
                                 payload, sig, valid_from)
        resp.version = version
        resp.published = True
        resp.valid_from_host_time = valid_from
        resp.valid_to_host_time = None
        resp.signature = sig

    @staticmethod
    def _wrap_count(modulus: Optional[float], unwrapped_c: float) -> int:
        if modulus is None:
            return 0
        return int(math.floor(unwrapped_c / modulus))

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def convert(self, device_id: str, raw_counter: float,
                hint: Optional[float], version: Optional[str]) -> ConvertResponse:
        if version:
            row = self.store.get_model(version)
            if row is None or json.loads(row["payload_json"])["device_id"] != device_id:
                return ConvertResponse(
                    device_id=device_id, version=version or "",
                    status="uncertain", in_validity_range=False,
                    reason="unknown calibration version for this device",
                )
        else:
            row = self.store.select_model_for_hint(device_id, hint)
        if row is None:
            return ConvertResponse(
                device_id=device_id, version="", status="uncertain",
                in_validity_range=False,
                reason="no published calibration for this device",
            )

        payload = json.loads(row["payload_json"])
        if not crypto.verify(payload, row["signature"], self.key):
            return ConvertResponse(
                device_id=device_id, version=row["version"], status="uncertain",
                in_validity_range=False,
                reason="stored model FAILED signature verification",
            )

        modulus = payload.get("counter_modulus")
        boot = np.array(payload.get("bootstrap", []), dtype=float)
        pts = payload.get("envelope_points", [])
        slope = float(payload["slope"])
        intercept = float(payload["intercept"])

        # Choose wrap count. With a host hint, unwrap so the prediction sits
        # nearest the hint; otherwise anchor at the fit region's wrap count.
        if modulus:
            base_count = int(payload.get("wrap_count_at_min", 0))
            fit_cmin = float(payload["fit_counter_min"])
            base_raw = fit_cmin - base_count * modulus

            def pred_for(k: int) -> float:
                return slope * (k * modulus + raw_counter) + intercept

            if hint is not None:
                # Unwrap: host ~= slope*(k*M + raw) + intercept  =>  k
                k_est = int(round(
                    (hint - intercept - slope * raw_counter) / (slope * modulus)))
                k_lo = max(0, k_est - 2)
                k_hi = k_est + 3
                k = min(range(k_lo, k_hi),
                        key=lambda kk: abs(pred_for(kk) - hint))
            else:
                # Closest unwrapped value to the fit region's raw anchor.
                k = max(0, int(round((fit_cmin - base_raw - raw_counter)
                                     / modulus)))
            c_unwrapped = k * modulus + raw_counter
        else:
            c_unwrapped = raw_counter

        # Statistical interval from bootstrap lines ...
        if len(boot):
            preds = boot[:, 0] * c_unwrapped + boot[:, 1]
            lo = float(np.quantile(preds, 0.025))
            hi = float(np.quantile(preds, 0.975))
        else:
            lo = hi = slope * c_unwrapped + intercept
        # ... widened by the asymmetric feasibility envelope.
        k_lo, k_hi = -math.inf, math.inf
        for p in pts:
            c, plo, phi = p["c"], p["lo"], p["hi"]
            if plo is not None:
                k_lo = max(k_lo, float(plo) - slope * c)
            if phi is not None:
                k_hi = min(k_hi, float(phi) - slope * c)
        if math.isfinite(k_lo):
            lo = min(lo, slope * c_unwrapped + k_lo)
        if math.isfinite(k_hi):
            hi = max(hi, slope * c_unwrapped + k_hi)
        point = slope * c_unwrapped + intercept

        in_range = (
            payload["fit_counter_min"] <= c_unwrapped <= payload["fit_counter_max"]
        ) or (
            payload["fit_host_min"] <= point <= payload["fit_host_max"]
        )
        status = "ok" if (row["status"] == "calibrated"
                          and math.isfinite(lo) and math.isfinite(hi)) else "uncertain"
        resp = ConvertResponse(
            device_id=device_id,
            version=row["version"],
            status=status,
            host_time_point=point if math.isfinite(point) else None,
            host_time_interval=Interval(lower=lo, upper=hi)
            if math.isfinite(lo) and math.isfinite(hi) else None,
            in_validity_range=bool(in_range),
            reason=None if in_range else
            "reading lies outside the calibration evidence range; extrapolated",
            signature=row["signature"],
        )
        self.store.log_conversion(device_id, row["version"], raw_counter,
                                 resp.host_time_point,
                                 resp.host_time_interval.lower
                                 if resp.host_time_interval else None,
                                 resp.host_time_interval.upper
                                 if resp.host_time_interval else None,
                                 status)
        return resp

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_models(self, device_id: Optional[str]) -> list[ModelSummary]:
        out = []
        for r in self.store.list_models(device_id):
            p = json.loads(r["payload_json"])
            c_lo, c_hi = p["fit_counter_min"], p["fit_counter_max"]
            c_mid = 0.5 * (c_lo + c_hi)
            offset = None
            if p.get("counter_nominal_hz"):
                offset = (p["slope"] * c_mid + p["intercept"]
                          ) - c_mid / p["counter_nominal_hz"]
            out.append(ModelSummary(
                version=r["version"],
                device_id=r["device_id"],
                status=r["status"],
                valid_from_host_time=r["valid_from"],
                valid_to_host_time=r["valid_to"],
                created_at=r["created_at"],
                slope=p["slope"],
                offset_seconds=offset,
                superseded=bool(r["superseded"]),
            ))
        return out


def _json_float(x: float) -> float:
    return x if math.isfinite(x) else None  # type: ignore[return-value]
