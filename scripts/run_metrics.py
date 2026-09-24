"""对全部合成场景（或指定场景）做库内分割并打印精确率/召回率表。

用法：
    PYTHONPATH=src python3 scripts/run_metrics.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from groundseg import GroundSegParams, evaluate, segment_points  # noqa: E402
from groundseg import scenes  # noqa: E402


def main() -> int:
    names = sys.argv[1:] or list(scenes.SCENES.keys())
    p = GroundSegParams()

    header = (f"{'scene':18s} {'N':>6s} {'decBlk':>7s} "
              f"{'P_g':>6s} {'R_g':>6s} {'dR_g':>6s} {'und%':>6s} "
              f"{'P_ng':>6s} {'R_ng':>6s}")
    print(header)
    print("-" * len(header))
    rc = 0
    for name in names:
        if name not in scenes.SCENES:
            print(f"{'(unknown)':18s} {name}")
            rc = 2
            continue
        pts, truth, _ = scenes.SCENES[name]()
        res = segment_points(pts, p)
        m = evaluate(res.labels, truth.tolist())
        g, ng = m["ground"], m["non_ground"]
        print(f"{name:18s} {pts.shape[0]:6d} "
              f"{res.stats['decided_blocks']:3d}/{len(res.blocks):3d} "
              f"{g['precision']:6.3f} {g['recall']:6.3f} "
              f"{g['decided_recall']:6.3f} {g['undecided_rate']*100:5.1f}% "
              f"{ng['precision']:6.3f} {ng['recall']:6.3f}")
    print()
    print("P_g/R_g  = 地面点精确率/召回率（undecidable 在召回率中按漏判计）")
    print("dR_g     = 剔除 undecidable 后的判定内召回率")
    print("P_ng/R_ng= 非地面点精确率/召回率（对称指标）")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
