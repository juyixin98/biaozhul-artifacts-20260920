"""Run the bundled example instances with the CBS solver and print results.

Usage:  python -m examples.run_examples
"""

from __future__ import annotations

import json

from app.brute_force import brute_force_optimal_cost
from app.cbs import solve_cbs, validate_solution
from app.grid import GridMap
from examples.instances import ALL


def run_instance(name: str, spec: dict) -> dict:
    grid = GridMap(
        spec["grid"]["width"],
        spec["grid"]["height"],
        [tuple(o) for o in spec["grid"]["obstacles"]],
    )
    starts = [tuple(a["start"]) for a in spec["agents"]]
    goals = [tuple(a["goal"]) for a in spec["agents"]]

    solution = solve_cbs(grid, starts, goals)
    brute = brute_force_optimal_cost(grid, starts, goals)

    report: dict = {"instance": name}
    if solution is None:
        report["status"] = "unsolvable"
        report["brute_force_cost"] = brute
        report["agrees_with_brute_force"] = brute is None
        return report

    errors = validate_solution(grid, starts, goals, solution.paths)
    report.update(
        status="solved",
        cost=solution.cost,
        makespan=solution.makespan,
        brute_force_cost=brute,
        agrees_with_brute_force=(brute == solution.cost),
        replay_errors=errors,
        stats=solution.stats.as_dict(),
        timetable={
            spec["agents"][i]["id"]: [[x, y, t] for t, (x, y) in enumerate(solution.paths[i])]
            for i in range(len(starts))
        },
    )
    return report


def main() -> None:
    for name, spec in ALL.items():
        print(json.dumps(run_instance(name, spec), indent=2))


if __name__ == "__main__":
    main()
