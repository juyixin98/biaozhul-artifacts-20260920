"""Acceptance experiments for the dual-tone detection service.

Sweeps (all on synthetic signals — no real-line claims):
  1. Frequency offset sweep: how much oscillator error is tolerated.
  2. SNR sweep: detection rate vs additive white noise level.
  3. Adjacent-tone switching: minimal inter-digit gap.
  4. Confusion matrix over all 16 keys at a fixed operating point.
  5. Rejection behaviour: silence, pure noise, single tones, short bursts.

Writes machine-readable JSON and a Markdown summary to ``results/``.
Run:  python -m experiments.acceptance
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from dtmf_service.detector import DetectorConfig, DTMFDetector
from dtmf_service.synth import ALL_KEYS, synthesize_sequence, synthesize_tone

RATE = 8000
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

OFFSETS_PCT = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
SNRS_DB = [None, 30.0, 20.0, 15.0, 10.0, 5.0, 0.0, -5.0]
GAPS_MS = [100.0, 60.0, 40.0, 30.0, 20.0, 10.0]
TRIALS_PER_CELL = 4  # seeds per (key, condition) cell


def _detect(samples, **cfg):
    detector = DTMFDetector(DetectorConfig(sample_rate=RATE, **cfg))
    return detector.detect(samples, RATE)


def sweep_offsets() -> list[dict]:
    rows = []
    for offset in OFFSETS_PCT:
        correct = total = 0
        for key in ALL_KEYS:
            for seed in range(TRIALS_PER_CELL):
                samples = synthesize_sequence(
                    key, RATE, tone_ms=100, gap_ms=50,
                    freq_offset_pct=offset, snr_db=20.0, seed=seed,
                )
                total += 1
                correct += _detect(samples).digits == key
        rows.append({
            "offset_pct": offset,
            "accuracy": round(correct / total, 4),
            "trials": total,
        })
    return rows


def sweep_snr() -> list[dict]:
    rows = []
    for snr in SNRS_DB:
        correct = total = 0
        for key in ALL_KEYS:
            for seed in range(TRIALS_PER_CELL):
                samples = synthesize_sequence(
                    key, RATE, tone_ms=100, gap_ms=50, snr_db=snr, seed=seed,
                )
                total += 1
                correct += _detect(samples).digits == key
        rows.append({
            "snr_db": "clean" if snr is None else snr,
            "accuracy": round(correct / total, 4),
            "trials": total,
        })
    return rows


def sweep_gaps() -> list[dict]:
    rows = []
    sequence = "123456789*0#"
    for gap in GAPS_MS:
        correct = total = 0
        for seed in range(TRIALS_PER_CELL):
            samples = synthesize_sequence(
                sequence, RATE, tone_ms=80, gap_ms=gap, snr_db=20.0, seed=seed,
            )
            total += 1
            correct += _detect(samples).digits == sequence
        rows.append({
            "gap_ms": gap,
            "sequence_accuracy": round(correct / total, 4),
            "trials": total,
        })
    return rows


def sweep_same_key_gaps() -> list[dict]:
    """Same key pressed twice: gaps below ``min_gap_ms`` merge into one press."""
    rows = []
    for gap in GAPS_MS:
        got: dict[str, int] = {}
        for seed in range(TRIALS_PER_CELL):
            samples = synthesize_sequence(
                "77", RATE, tone_ms=80, gap_ms=gap, snr_db=20.0, seed=seed,
            )
            digits = _detect(samples).digits
            got[digits] = got.get(digits, 0) + 1
        rows.append({
            "gap_ms": gap,
            "decoded": got,
            "trials": TRIALS_PER_CELL,
        })
    return rows


def confusion_matrix() -> dict:
    keys = list(ALL_KEYS)
    matrix = {k: {t: 0 for t in keys + ["<reject>"]} for k in keys}
    for key in keys:
        for seed in range(TRIALS_PER_CELL):
            samples = synthesize_sequence(
                key, RATE, tone_ms=100, gap_ms=50, snr_db=20.0, seed=seed,
            )
            got = _detect(samples).digits
            matrix[key][got if got in matrix[key] else "<reject>"] += 1
    return {"keys": keys + ["<reject>"], "rows": matrix}


def rejection_report() -> list[dict]:
    rng = np.random.default_rng(0)
    cases: list[tuple[str, np.ndarray]] = [
        ("silence", np.zeros(RATE)),
        ("white_noise", rng.normal(0, 0.3, RATE)),
        (
            "single_low_tone",
            np.sin(2 * np.pi * 697.0 * np.arange(RATE // 5) / RATE) * 0.5,
        ),
        (
            "short_burst_20ms",
            synthesize_sequence("5", RATE, tone_ms=20, gap_ms=50, seed=0),
        ),
        (
            "extreme_twist_+20dB",
            synthesize_tone("5", RATE, duration_ms=100, twist_db=20.0),
        ),
    ]
    rows = []
    for name, samples in cases:
        result = _detect(samples)
        reasons = sorted({r.reason for r in result.rejections})
        rows.append({
            "case": name,
            "digits": result.digits or "<none>",
            "rejected": result.digits == "",
            "reasons": reasons,
        })
    return rows


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    report = {
        "sample_rate": RATE,
        "trials_per_cell": TRIALS_PER_CELL,
        "frequency_offset_sweep": sweep_offsets(),
        "snr_sweep": sweep_snr(),
        "adjacent_gap_sweep": sweep_gaps(),
        "same_key_gap_sweep": sweep_same_key_gaps(),
        "confusion_matrix": confusion_matrix(),
        "rejection_cases": rejection_report(),
    }
    out = RESULTS_DIR / "acceptance.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md = RESULTS_DIR / "acceptance.md"
    md.write_text(_to_markdown(report), encoding="utf-8")
    print(f"wrote {out} and {md}")


def _to_markdown(report: dict) -> str:
    lines = ["# Acceptance results (synthetic signals only)", ""]
    lines.append("## Frequency offset sweep (SNR 20 dB)")
    lines.append("| offset % | accuracy | trials |")
    lines.append("|---|---|---|")
    for r in report["frequency_offset_sweep"]:
        lines.append(f"| {r['offset_pct']} | {r['accuracy']} | {r['trials']} |")
    lines.append("\n## SNR sweep")
    lines.append("| SNR dB | accuracy | trials |")
    lines.append("|---|---|---|")
    for r in report["snr_sweep"]:
        lines.append(f"| {r['snr_db']} | {r['accuracy']} | {r['trials']} |")
    lines.append("\n## Adjacent-tone switching (sequence 123456789*0#)")
    lines.append("| gap ms | sequence accuracy | trials |")
    lines.append("|---|---|---|")
    for r in report["adjacent_gap_sweep"]:
        lines.append(f"| {r['gap_ms']} | {r['sequence_accuracy']} | {r['trials']} |")
    lines.append("\n## Same-key re-press (sequence 77, min_gap_ms=30)")
    lines.append("| gap ms | decoded counts | trials |")
    lines.append("|---|---|---|")
    for r in report["same_key_gap_sweep"]:
        decoded = ", ".join(f"'{k}'x{v}" for k, v in sorted(r["decoded"].items()))
        lines.append(f"| {r['gap_ms']} | {decoded} | {r['trials']} |")
    cm = report["confusion_matrix"]
    lines.append("\n## Confusion matrix (SNR 20 dB, rows=truth, cols=detected)")
    header = "| truth \\ detected | " + " | ".join(cm["keys"]) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(cm["keys"]) + 1))
    for truth, row in cm["rows"].items():
        lines.append(f"| {truth} | " + " | ".join(str(row[c]) for c in cm["keys"]) + " |")
    lines.append("\n## Rejection cases")
    lines.append("| case | digits | rejected | reasons |")
    lines.append("|---|---|---|---|")
    for r in report["rejection_cases"]:
        lines.append(
            f"| {r['case']} | {r['digits']} | {r['rejected']} | {', '.join(r['reasons']) or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
