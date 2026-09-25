"""Request-driven offline processing service.

A request is a plain dict (typically loaded from JSON)::

    {
      "filter": {
        "type": "butter_lowpass" | "cheby1_lowpass" | "sos",
        "order": 4, "cutoff": 0.2, "ripple_db": 1.0,
        "sections": [[b0,b1,b2,1,a1,a2], ...]
      },
      "fixed": {"sig": [1,15], "coef": [2,14], "guard_bits": 2,
                "rounding": "convergent"},
      "input": {"kind": "sine"|"multi_tone"|"chirp"|"impulse"|"noise"|"pcm",
                "n": 4096, "amplitude": 0.9, "freq_fraction": 0.05,
                "components": [[f,a], ...], "path": "in.pcm", "dtype": "int16"},
      "output": {"dir": "out", "basename": "run", "pcm": true,
                 "csv": true, "npy": false, "dtype": "int16"},
      "analyze": true
    }

Only numbers and files are returned -- there is no UI or playback.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

import numpy as np

from . import signals as sigmod
from .analysis import run_acceptance
from .biquad import FilterConfig, SOS, filter_fixed, filter_float
from .design import butter_lowpass, cheby1_lowpass
from .qformat import QFormat
from .stability import assess_stability


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _build_filter(spec: dict[str, Any]) -> SOS:
    kind = str(spec.get("type", "butter_lowpass"))
    if kind == "butter_lowpass":
        return butter_lowpass(int(spec["order"]), float(spec["cutoff"]))
    if kind == "cheby1_lowpass":
        return cheby1_lowpass(
            int(spec["order"]), float(spec["cutoff"]),
            float(spec.get("ripple_db", 1.0)),
        )
    if kind == "sos":
        rows = np.asarray(spec["sections"], dtype=np.float64)
        if rows.shape[1] == 6:
            return SOS(rows)
        raise ValueError("sections rows must each contain 6 values [b0,b1,b2,a0,a1,a2]")
    raise ValueError(f"unknown filter type {kind!r}")


def _build_config(spec: dict[str, Any] | None) -> FilterConfig:
    spec = spec or {}
    sig_iv = spec.get("sig", [1, 15])
    coef_iv = spec.get("coef", [2, 14])
    return FilterConfig(
        q_sig=QFormat(int(sig_iv[0]), int(sig_iv[1])),
        q_coef=QFormat(int(coef_iv[0]), int(coef_iv[1])),
        guard_bits=int(spec.get("guard_bits", 2)),
        rounding=str(spec.get("rounding", "convergent")),
        overflow=str(spec.get("overflow", "saturate")),
    )


def _build_input(spec: dict[str, Any]) -> tuple[np.ndarray, str]:
    kind = str(spec.get("kind", "sine"))
    n = int(spec.get("n", 4096))
    amp = float(spec.get("amplitude", 0.9))
    if kind == "sine":
        return sigmod.sine(n, float(spec["freq_fraction"]), amp), kind
    if kind == "multi_tone":
        comps = [(float(f), float(a)) for f, a in spec["components"]]
        return sigmod.multi_tone(n, comps), kind
    if kind == "chirp":
        return sigmod.chirp(n, float(spec.get("f0", 0.0)),
                            float(spec.get("f1", 0.5)), amp), kind
    if kind == "impulse":
        return sigmod.impulse(n, amp, int(spec.get("at", 0))), kind
    if kind == "noise":
        return sigmod.noise(n, amp, int(spec.get("seed", 0))), kind
    if kind == "pcm":
        return sigmod.read_pcm(spec["path"], str(spec.get("dtype", "int16"))), kind
    raise ValueError(f"unknown input kind {kind!r}")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _samples_preview(y: np.ndarray, k: int = 8) -> dict[str, list[float]]:
    if y.size <= 2 * k:
        vals = y.tolist()
        return {"all": vals}
    return {"head": y[:k].tolist(), "tail": y[-k:].tolist()}


def _stability_dict(rep) -> dict[str, Any]:
    secs = []
    for sf, sq in zip(rep.poles_float.sections, rep.poles_quantized.sections):
        secs.append({
            "section": sf.index,
            "float_pole_radii": sf.radii,
            "quantized_pole_radii": sq.radii,
            "quantized_poles": [{"real": p.real, "imag": p.imag} for p in sq.poles],
            "jury_stable_quantized": sq.jury_stable,
            "jury_values_quantized": sq.jury_values,
        })
    cycles = [
        {
            "section": c.section_index,
            "found": c.found,
            "kind": c.kind,
            "amplitude": c.amplitude,
            "period": c.period,
            "tail_q_sig": c.tail,
            "max_state_fraction": c.max_growth,
        }
        for c in rep.limit_cycles
    ]
    return {
        "verdict": rep.verdict,
        "float_stable": rep.stable_float,
        "max_pole_radius_float": rep.poles_float.max_radius,
        "max_pole_radius_quantized": rep.poles_quantized.max_radius,
        "unstable_sections_quantized": rep.poles_quantized.unstable_sections,
        "coefficient_saturations": rep.coefficient_saturations,
        "coefficient_max_abs_error": rep.max_coefficient_error,
        "sections": secs,
        "limit_cycles": cycles,
        "warnings": rep.warnings,
    }


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def process_request(req: dict[str, Any]) -> dict[str, Any]:
    """Validate and run one request; returns a JSON-serializable response."""
    try:
        sos = _build_filter(req.get("filter", {}))
        cfg = _build_config(req.get("fixed"))
        x, input_kind = _build_input(req.get("input", {}))
        out_spec = req.get("output", {}) or {}
        do_acceptance = bool(req.get("analyze", False))

        ref = filter_float(x, sos)
        res = filter_fixed(x, sos, cfg, reference=ref)
        err = res.y_fixed_float - res.y_reference

        out_dir = out_spec.get("dir", ".")
        base = out_spec.get("basename", "fixed_iir_out")
        os.makedirs(out_dir, exist_ok=True)
        files: dict[str, str] = {}
        if out_spec.get("pcm", False):
            p = os.path.join(out_dir, base + ".pcm")
            sigmod.write_pcm(p, res.y_fixed_float, str(out_spec.get("dtype", "int16")))
            files["pcm"] = p
        if out_spec.get("csv", False):
            p = os.path.join(out_dir, base + ".csv")
            sigmod.write_csv(p, res.y_fixed_float)
            files["csv"] = p
        if out_spec.get("npy", False):
            p = os.path.join(out_dir, base + ".npy")
            np.save(p, res.y_fixed_float)
            files["npy"] = p
        # Always emit the float reference and integer output alongside as .npy
        # so callers can audit the numeric result offline.
        ref_path = os.path.join(out_dir, base + ".reference.npy")
        int_path = os.path.join(out_dir, base + ".fixed_int.npy")
        np.save(ref_path, res.y_reference)
        np.save(int_path, res.y_fixed_int)
        files["reference_npy"] = ref_path
        files["fixed_int_npy"] = int_path

        response: dict[str, Any] = {
            "ok": True,
            "input": {
                "kind": input_kind,
                "n_samples": int(x.size),
                "max_abs": float(np.max(np.abs(x))) if x.size else 0.0,
            },
            "filter": {
                "n_sections": sos.n_sections,
                "sections_float": sos.sections.tolist(),
                "sections_quantized": res.quantized.to_float_sos().sections.tolist(),
                "coefficient_quantization": {
                    "q_coef": str(cfg.q_coef),
                    "rounding": cfg.rounding,
                    "saturated_count": res.quantized.saturated_count,
                    "max_abs_err": res.quantized.max_abs_err,
                },
            },
            "fixed_point": {
                "q_sig": str(cfg.q_sig),
                "q_state": str(cfg.q_state),
                "guard_bits": cfg.guard_bits,
                "rounding": cfg.rounding,
                "overflow": cfg.overflow,
            },
            "output": {
                "files": files,
                "max_abs": float(np.max(np.abs(res.y_fixed_float))),
                "input_saturations": res.input_saturations,
                "state_saturations_total": res.total_state_saturations,
                "state_saturations_per_section": res.state_saturations.tolist(),
                "output_saturations": res.output_saturations,
                "vs_float_reference": {
                    "max_abs_error": float(np.max(np.abs(err))),
                    "rms_error": float(np.sqrt(np.mean(err**2))),
                },
                "samples": _samples_preview(res.y_fixed_float),
            },
        }
        stab = assess_stability(sos, cfg)
        response["stability"] = _stability_dict(stab)

        if do_acceptance:
            acc = run_acceptance(sos, cfg)
            response["acceptance"] = {
                "impulse": {
                    "max_abs_error": acc.impulse.max_abs_error,
                    "rms_error": acc.impulse.rms_error,
                    "tail_rms": acc.impulse.tail_rms,
                    "state_saturations": acc.impulse.state_saturations,
                    "output_saturations": acc.impulse.output_saturations,
                },
                "large_signal": {
                    "amplitude": acc.large.amplitude,
                    "input_saturations": acc.large.input_saturations,
                    "state_saturations": acc.large.state_saturations,
                    "output_saturations": acc.large.output_saturations,
                    "clipped_samples": acc.large.clipped_samples,
                    "max_abs_error": acc.large.max_abs_error_vs_float_scaled,
                },
                "frequency_response": {
                    "max_mag_db_error": acc.freq.max_mag_db_error,
                    "max_phase_deg_error": acc.freq.max_phase_deg_error,
                    "bands": [asdict(b) for b in acc.freq.bands],
                },
            }
        return response
    except (KeyError, ValueError, TypeError, OSError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def process_request_file(request_path: str, response_path: str | None = None) -> dict[str, Any]:
    """Load a JSON request, process it, persist the response JSON, return it."""
    with open(request_path, "r", encoding="utf-8") as fh:
        req = json.load(fh)
    resp = process_request(req)
    if response_path is None:
        response_path = os.path.join(
            req.get("output", {}).get("dir", "."),
            req.get("output", {}).get("basename", "fixed_iir_out") + ".response.json",
        )
    os.makedirs(os.path.dirname(os.path.abspath(response_path)), exist_ok=True)
    with open(response_path, "w", encoding="utf-8") as fh:
        json.dump(resp, fh, indent=2)
    return resp
