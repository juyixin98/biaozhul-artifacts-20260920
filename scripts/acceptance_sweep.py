"""Acceptance sweep: non-integer-frequency tones, two close tones, boundaries.

Writes results/acceptance_single_tone.csv, results/acceptance_two_tone.csv
and results/acceptance_summary.json. Run from the repo root:

  python scripts/acceptance_sweep.py
"""

from __future__ import annotations

import csv
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from spectral_peak import analyze, multitone, tone  # noqa: E402

SR = 48000.0
N = 4096
BIN_HZ = SR / N
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")


def _strongest_nonboundary(result: dict) -> dict | None:
    interior = [p for p in result["peaks"] if not p["boundary"]]
    if not interior:
        return None
    return max(interior, key=lambda p: p["amplitude"])


def sweep_single_tone() -> list[dict]:
    """Non-integer-bin tones from 2.5 bins to Nyquist-2.5 bins."""
    rows = []
    n_steps = 200
    f_lo, f_hi = 2.5 * BIN_HZ, (N // 2 - 2.5) * BIN_HZ
    for i in range(n_steps):
        # Irrational-ish spacing keeps every tone off an integer bin.
        freq = f_lo + (f_hi - f_lo) * (i + 0.37) / n_steps
        x = tone(freq, amplitude=0.8, sample_rate=SR, n=N, phase=0.7)
        peak = _strongest_nonboundary(analyze(x, SR))
        assert peak is not None, f"no interior peak for {freq} Hz"
        rows.append(
            {
                "true_freq_hz": freq,
                "est_freq_hz": peak["freq_hz"],
                "freq_err_hz": peak["freq_hz"] - freq,
                "freq_err_bins": (peak["freq_hz"] - freq) / BIN_HZ,
                "true_amp": 0.8,
                "est_amp": peak["amplitude"],
                "amp_err_rel": peak["amplitude"] / 0.8 - 1.0,
                "interference": peak["interference"],
            }
        )
    return rows


def sweep_two_tone() -> list[dict]:
    """Two equal tones; separation swept across the Hann mainlobe (4 bins)."""
    rows = []
    f1 = 100.3 * BIN_HZ
    for sep_bins in (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 12.0):
        f2 = f1 + sep_bins * BIN_HZ
        x = multitone([(f1, 0.8), (f2, 0.8)], sample_rate=SR, n=N)
        result = analyze(x, SR)
        interior = [p for p in result["peaks"] if not p["boundary"]]
        ests = sorted(p["freq_hz"] for p in interior[:2])
        err1 = abs(ests[0] - f1) / BIN_HZ if len(ests) > 0 else float("nan")
        err2 = abs(ests[1] - f2) / BIN_HZ if len(ests) > 1 else float("nan")
        rows.append(
            {
                "sep_bins": sep_bins,
                "n_peaks_found": len(interior),
                "freq_err_bins_tone1": err1,
                "freq_err_bins_tone2": err2,
                "interference_flagged": any(p["interference"] for p in interior),
            }
        )
    return rows


def boundary_cases() -> list[dict]:
    """DC and Nyquist tones: interpolation must be skipped, boundary flagged."""
    rows = []
    for name, x in (
        ("dc", np.full(N, 0.5)),
        # A Nyquist sine with phase 0 samples to all zeros; use phase pi/2.
        ("nyquist", tone(SR / 2, amplitude=0.8, sample_rate=SR, n=N,
                         phase=np.pi / 2)),
        ("near_dc_1.3bins", tone(1.3 * BIN_HZ, amplitude=0.8, sample_rate=SR, n=N)),
        (
            "near_nyquist_1.3bins",
            tone((N // 2 - 1.3) * BIN_HZ, amplitude=0.8, sample_rate=SR, n=N),
        ),
    ):
        result = analyze(x, SR)
        top = max(result["peaks"], key=lambda p: p["amplitude"])
        rows.append(
            {
                "case": name,
                "bin": top["bin"],
                "freq_hz": top["freq_hz"],
                "amplitude": top["amplitude"],
                "boundary": top["boundary"],
                "estimator": top["estimator"],
            }
        )
    return rows


def _stats(errors: np.ndarray) -> dict:
    return {
        "max_abs": float(np.max(np.abs(errors))),
        "rms": float(np.sqrt(np.mean(errors**2))),
        "mean": float(np.mean(errors)),
    }


def main() -> int:
    os.makedirs(RESULTS_DIR, exist_ok=True)

    single = sweep_single_tone()
    two = sweep_two_tone()
    bounds = boundary_cases()

    freq_err_bins = np.array([r["freq_err_bins"] for r in single])
    amp_err_rel = np.array([r["amp_err_rel"] for r in single])
    summary = {
        "config": {"sample_rate": SR, "n": N, "bin_hz": BIN_HZ,
                   "window": "hann", "estimator": "auto (hann_ratio)"},
        "single_tone": {
            "n_cases": len(single),
            "freq_err_bins": _stats(freq_err_bins),
            "freq_err_hz": _stats(freq_err_bins * BIN_HZ),
            "amp_err_rel": _stats(amp_err_rel),
            "interference_false_positives": sum(r["interference"] for r in single),
        },
        "two_tone": two,
        "boundary": bounds,
    }

    def write_csv(name: str, rows: list[dict]) -> None:
        path = os.path.join(RESULTS_DIR, name)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    write_csv("acceptance_single_tone.csv", single)
    write_csv("acceptance_two_tone.csv", two)
    write_csv("acceptance_boundary.csv", bounds)
    with open(os.path.join(RESULTS_DIR, "acceptance_summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print(json.dumps(summary["single_tone"], indent=2))
    print("two-tone and boundary tables written to results/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
