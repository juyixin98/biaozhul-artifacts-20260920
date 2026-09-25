"""离线生成 HTTP 请求样例文件（可复现，不依赖网络）。

运行：``python examples/generate_examples.py``，在 examples/ 下写入 JSON。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift import synthetic  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _write(name: str, payload: dict) -> None:
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"wrote {path} ({os.path.getsize(path)} bytes)")


def main() -> None:
    b_same, c_same = synthetic.same_distribution(seed=42)
    b_shift, c_shift = synthetic.shifted_distribution(mean_shift=1.0, seed=42)
    _, c_missing = synthetic.same_distribution(seed=42)
    c_missing = synthetic.inject_missing(c_missing, rate=0.3, seed=9)

    _write("monitor_same.json", {
        "n_bins": 10,
        "smoothing": "laplace",
        "alpha": 0.5,
        "min_sample": 30,
        "baseline": {"value": b_same.tolist()},
        "current": {"value": c_same.tolist()},
    })
    _write("monitor_shifted.json", {
        "n_bins": 10,
        "smoothing": "laplace",
        "baseline": {"value": b_shift.tolist()},
        "current": {"value": c_shift.tolist()},
    })
    _write("monitor_missing.json", {
        "n_bins": 10,
        "baseline": {"value": b_same.tolist()},
        "current": {"value": [None if v != v else v for v in c_missing.tolist()]},
    })
    _write("monitor_no_smoothing.json", {
        "n_bins": 10,
        "smoothing": "none",
        "baseline": {"value": b_same.tolist()},
        "current": {"value": c_shift.tolist()},
    })
    _write("baseline_create.json", {
        "n_bins": 10,
        "features": {"value": b_same.tolist()},
    })
    _write("drift_current_shifted.json", {
        "smoothing": "laplace",
        "current": {"value": c_shift.tolist()},
    })
    _write("demo_shifted.json", {"scenario": "shifted"})
    _write("demo_small_sample.json", {"scenario": "small_sample"})
    _write("demo_all_missing.json", {"scenario": "all_missing"})


if __name__ == "__main__":
    main()
