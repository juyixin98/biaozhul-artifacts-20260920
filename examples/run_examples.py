"""离线示例回放：交叉口、让行港湾、单通道无解。

直接运行：python examples/run_examples.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mrcbs.brute_force import brute_force_solve
from mrcbs.cbs import pad_timetable, solve_cbs
from mrcbs.grid import parse_grid
from mrcbs.validate import validate_timetable

SCENARIOS = [
    {
        "name": "交叉口（3x3，两机器人垂直穿越）",
        "grid": ["...", "...", "..."],
        "starts": [(1, 0), (0, 1)],
        "goals": [(1, 2), (2, 1)],
    },
    {
        "name": "带让行港湾的单通道（有解）",
        "grid": ["...", "#.#"],
        "starts": [(0, 0), (0, 2)],
        "goals": [(0, 2), (0, 0)],
    },
    {
        "name": "窄单通道对向互换（无解）",
        "grid": ["..."],
        "starts": [(0, 0), (0, 2)],
        "goals": [(0, 2), (0, 0)],
    },
    {
        "name": "目标格被先到者永久占用（无解）",
        "grid": ["...."],
        "starts": [(0, 0), (0, 2)],
        "goals": [(0, 1), (0, 0)],
    },
]


def main() -> None:
    for sc in SCENARIOS:
        print(f"\n=== {sc['name']} ===")
        grid = parse_grid(sc["grid"])
        result = solve_cbs(grid, sc["starts"], sc["goals"])
        print(f"CBS 状态: {result.status}, 总代价: {result.cost}")
        print(f"约束树统计: {json.dumps(result.stats, ensure_ascii=False)}")
        bf = brute_force_solve(grid, sc["starts"], sc["goals"])
        if bf is None:
            print("穷举核对: 同样无解")
        else:
            print(f"穷举核对: 最小总代价 {bf[0]} -> "
                  f"{'一致' if bf[0] == result.cost else '不一致!'}")
        if result.paths is not None:
            timetable = pad_timetable(result.paths)
            violations = validate_timetable(grid, sc["starts"], sc["goals"], timetable)
            print(f"时刻表校验: {'通过' if not violations else violations}")
            for i, path in enumerate(timetable):
                print(f"  机器人 {i}: " + " -> ".join(str(c) for c in path))


if __name__ == "__main__":
    main()
