"""本地验收：运行全部离线夹具并打印 ID 切换与位置误差表。

用法：
    python scripts/run_evaluation                 # 表格
    python scripts/run_evaluation --json          # JSON
    python scripts/run_evaluation --noisy --seed 7
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evaluation import all_scenarios, run_scenario  # noqa: E402
from app.tracker import TrackerConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="多目标轨迹关联离线评测")
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument(
        "--noisy",
        action="store_true",
        help="对提交检测额外加 sigma=0.15 的高斯噪声（真值仍为无噪轨迹）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--confirm-hits", type=int, default=3)
    parser.add_argument("--max-misses", type=int, default=5)
    args = parser.parse_args()

    cfg = TrackerConfig(
        confirm_hits=args.confirm_hits,
        max_misses=args.max_misses,
        measurement_var=0.15**2 if args.noisy else 1.0,
        process_noise=1.0,
    )
    rows = []
    for scenario in all_scenarios(seed=args.seed):
        metrics, _ = run_scenario(
            scenario,
            config=cfg,
            measurement_std=0.15 if args.noisy else 0.0,
            seed=args.seed,
        )
        rows.append(metrics.to_dict())

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    header = (
        f"{'scenario':<16}{'ID切换':>8}{'RMSE':>10}{'平均误差':>10}"
        f"{'最大误差':>10}{'确认ID':>12}{'新生':>6}{'删除':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['scenario']:<16}{r['id_switches']:>8}{r['rmse']:>10.4f}"
            f"{r['mean_error']:>10.4f}{r['max_error']:>10.4f}"
            f"{str(r['confirmed_ids_used']):>12}{r['births_total']:>6}"
            f"{r['deleted_total']:>6}"
        )
    # 退出码：短时遮挡（gap4）/交叉/重复/空帧不得出现身份切换
    forbidden = {
        "crossing",
        "occlusion_gap4",
        "duplicates",
        "empty_frames",
    }
    bad = [r for r in rows if r["scenario"] in forbidden and r["id_switches"] != 0]
    noisy_bad_rmse = [r for r in rows if r["rmse"] > (0.35 if args.noisy else 0.1)]
    if bad or noisy_bad_rmse:
        print("验收失败：存在非预期 ID 切换或位置误差超阈值", file=sys.stderr)
        return 1
    print("\n验收通过：核心夹具 0 次非预期 ID 切换，RMSE 在阈值内。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
