"""纯库 API 演示（无需启动 HTTP 服务）：

    python examples/demo_library.py

覆盖：同分布、平移、空桶平滑对比、全缺失、小样本、缺值率漂移、溢出桶。
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift import synthetic  # noqa: E402
from drift.pipeline import monitor_feature  # noqa: E402


def show(title: str, result) -> None:
    d = result.to_dict()
    psi_text = f"{d['psi']:.4f}" if isinstance(d["psi"], float) else str(d["psi"])
    print(f"\n=== {title} ===")
    print(f"  PSI={psi_text}  JS={d['js_divergence']:.4f}  "
          f"TVD={d['tvd']:.4f}  band={d['psi_band']}")
    print(f"  n_base={d['n_baseline']} (observed {d['n_baseline_observed']}), "
          f"n_cur={d['n_current']} (observed {d['n_current_observed']})")
    print(f"  missing: base={d['missing_rate_baseline']:.3f} "
          f"cur={d['missing_rate_current']:.3f} delta={d['missing_rate_delta']:+.3f}")
    for note in d["notes"]:
        print(f"  note: {note}")


def main() -> None:
    b, c_same = synthetic.same_distribution(seed=42)
    _, c_shift = synthetic.shifted_distribution(mean_shift=1.0, seed=42)

    show("1. 同分布 N(0,1) vs N(0,1)", monitor_feature(b, c_same, "same"))
    show("2. 平移分布 N(0,1) vs N(1,1)", monitor_feature(b, c_shift, "shifted"))

    # 空桶：当前只落在最右桶。none/laplace/floor 三种平滑对比
    base = [0.0, 1.0, 2.0, 3.0] * 50
    cur = [3.0] * 200
    for method in ("none", "laplace", "floor"):
        show(f"3. 空桶场景 / smoothing={method}",
             monitor_feature(base, cur, "empty", n_bins=4, smoothing=method))

    bm, cm = synthetic.all_missing_current()
    show("4. 当前窗口全缺失", monitor_feature(bm, cm, "all_missing"))

    bs, cs = synthetic.small_sample(10)
    show("5. 小样本（n=10）", monitor_feature(bs, cs, "small"))

    _, c0 = synthetic.same_distribution(seed=42)
    cmiss = synthetic.inject_missing(c0, rate=0.4, seed=9)
    show("6. 缺值率漂移（当前 40% 缺失）",
         monitor_feature(b, cmiss, "missing_shift"))

    cext = synthetic.inject_extremes(c_same, rate=0.1, magnitude=50.0, seed=1)
    r = monitor_feature(b, cext, "extremes")
    show("7. 极端值进入溢出桶（10%）", r)
    labels = r.to_dict()["bins"]["labels"]
    cur_counts = r.to_dict()["bins"]["current_counts"]
    print(f"  逐桶: {dict(zip(labels, cur_counts))}")

    print("\n提醒：PSI 的 0.1/0.25 是工程经验阈值，不是统计检验结论。")


if __name__ == "__main__":
    main()
