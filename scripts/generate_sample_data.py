#!/usr/bin/env python3
"""Generate the committed example telemetry files (deterministic, no RNG).

Scenarios covered by examples/sample_telemetry.json (1 s nominal period):
  1. rest @ 4.02 V, 25 C          -> trusted rest window, OCV calibration
  2. 10 A discharge, 600 s        -> coulomb integration (discharge)
  3. 180 s sensor dropout         -> gap: no integration, uncertainty grows
  4. 10 A discharge, 300 s
  5. 20 samples @ -30 C           -> out-of-table temperature (flagged)
  6. 8 A charge, 300 s            -> charge direction + efficiency
  7. rest @ 3.78 V, 25 C          -> second OCV calibration

examples/late_samples.json targets a separate "replay-demo" session
(10 s spacing): one in-window late sample (accepted, triggers deterministic
recompute) and one stale sample older than the 3600 s replay horizon
(rejected, reported).
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "examples"


def s(t: float, i: float, v: float, temp: float) -> dict:
    return {"t_s": float(t), "current_a": float(i), "voltage_v": float(v), "temp_c": float(temp)}


def main_timeline() -> list[dict]:
    rows: list[dict] = []
    # 1. rest, t=0..119
    rows += [s(t, 0.0, 4.02, 25.0) for t in range(0, 120)]
    # 2. discharge 10 A, t=120..719
    rows += [s(t, 10.0, 3.95, 25.0) for t in range(120, 720)]
    # 3. GAP: t=720..899 missing (sensor dropout, 180 s)
    # 4. discharge 10 A, t=900..1199
    rows += [s(t, 10.0, 3.90, 25.0) for t in range(900, 1200)]
    # 5. out-of-table temperature -30 C, t=1200..1219, 5 A discharge
    rows += [s(t, 5.0, 3.55, -30.0) for t in range(1200, 1220)]
    # 6. charge -8 A, t=1220..1519
    rows += [s(t, -8.0, 4.05, 25.0) for t in range(1220, 1520)]
    # 7. rest @ 3.78 V, t=1520..1699
    rows += [s(t, 0.0, 3.78, 25.0) for t in range(1520, 1700)]
    return rows


def replay_timeline() -> list[dict]:
    # rest @ 3.78 V, 10 s spacing, t=0..4000 (gaps expected vs 5 s threshold;
    # that is fine — this session exists to demo the replay window)
    return [s(t, 0.0, 3.78, 25.0) for t in range(0, 4001, 10)]


def main() -> None:
    OUT.mkdir(exist_ok=True)

    (OUT / "create_session_demo.json").write_text(json.dumps({
        "session_id": "demo", "initial_soc": 0.8,
    }, indent=2) + "\n")

    (OUT / "sample_telemetry.json").write_text(json.dumps({
        "samples": main_timeline(),
    }, indent=1) + "\n")

    (OUT / "create_session_replay.json").write_text(json.dumps({
        "session_id": "replay-demo", "initial_soc": 0.6,
    }, indent=2) + "\n")

    (OUT / "replay_telemetry.json").write_text(json.dumps({
        "samples": replay_timeline(),
    }, indent=1) + "\n")

    # t_max after replay_telemetry = 4000 -> stale floor = 4000 - 3600 = 400
    # (timestamps deliberately off the 10 s sample grid so they are not
    # treated as duplicates)
    (OUT / "late_samples.json").write_text(json.dumps({
        "samples": [
            s(2005.0, 0.0, 3.78, 25.0),   # inside window -> accepted, recompute
            s(105.0, 0.0, 3.78, 25.0),    # older than horizon -> rejected as stale
        ],
    }, indent=1) + "\n")

    print("wrote examples/: create_session_demo.json, sample_telemetry.json,")
    print("                 create_session_replay.json, replay_telemetry.json, late_samples.json")


if __name__ == "__main__":
    main()
