"""离线命令行：对 JSON 请求文件执行轨迹平滑，输出结果并落盘 runs/。

用法：
    python run_smooth.py examples/narrow_corridor.json
    python run_smooth.py examples/angle_cutting.json --print-json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.smoother import smooth_trajectory


def main() -> int:
    parser = argparse.ArgumentParser(description="离线约束轨迹平滑")
    parser.add_argument("request", type=Path, help="请求 JSON 文件")
    parser.add_argument("--print-json", action="store_true", help="完整 JSON 输出")
    args = parser.parse_args()

    req = json.loads(args.request.read_text(encoding="utf-8"))
    resp = smooth_trajectory(req)

    if args.print_json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
    else:
        print(f"success          : {resp['success']}")
        print(f"result           : {resp['result']} ({resp['reason']})")
        print(f"n_points         : {resp['n_points']} (unique {resp['n_unique_consecutive']})")
        if resp["solve_runs"]:
            m = resp.get("metrics", {})
            print(f"iterations (total): {m.get('iterations_total')}")
            print(f"objective  : {m.get('objective_initial'):.6g} -> {m.get('objective_final'):.6g}")
            rw = m.get("residuals_world", {})
            print(f"deviation max       : {rw.get('deviation_max'):.6g} "
                  f"(bound {req['deviation_bound']:g})")
            print(f"curvature max       : {rw.get('curvature_max'):.6g} "
                  f"(limit {req['max_curvature']:g})")
            clr = rw.get("obstacle_sample_min_clearance")
            clr_text = "n/a (无障碍)" if clr is None else f"{clr:.6g}"
            print(f"sample min clearance: {clr_text} "
                  f"(margin {req.get('safety_margin', 0):g})")
        print(f"trajectory min clearance: {resp['min_clearance_world']}")
        print(f"elapsed seconds  : {resp['elapsed_seconds']:.4f}")
        print(f"integrity sha256 : {resp['integrity_sha256']}")

    return 0 if resp["success"] else 2


if __name__ == "__main__":
    sys.exit(main())
