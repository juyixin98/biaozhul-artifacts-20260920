#!/usr/bin/env python3
"""Offline CLI: read a request JSON file, write the full report to disk.

Usage:
    python scripts/run_offline.py examples/sample_request.json out/report.json
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imu_bias_estimator.models import BiasEstimateRequest, EstimateResponse
from imu_bias_estimator.pipeline import run


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__)
        return 2
    in_path, out_path = argv[1], argv[2] if len(argv) == 3 else None
    with open(in_path) as f:
        payload = json.load(f)
    req = BiasEstimateRequest(**payload)
    report = EstimateResponse(**run(req))
    text = report.model_dump_json(indent=2)
    if out_path:
        with open(out_path, "w") as f:
            f.write(text)
        print(f"report written to {out_path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
