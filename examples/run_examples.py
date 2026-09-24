"""Run the three benchmark scenarios offline and print a summary.

Usage:  python -m examples.run_examples
"""

from __future__ import annotations

import math

from hybrid_astar import GridMap, HybridAStarPlanner, PlannerConfig, Vehicle
from hybrid_astar.scenarios import narrow_corridor, reverse_uturn, unsolvable
from hybrid_astar.validate import check_collision_free, check_kinematics


def run(scenario, cfg: PlannerConfig | None = None, vehicle: Vehicle | None = None):
    grid = GridMap(scenario.grid, resolution=scenario.resolution)
    vehicle = vehicle or Vehicle()
    planner = HybridAStarPlanner(grid, vehicle, cfg or PlannerConfig())
    return planner.plan(scenario.start, scenario.goal), vehicle


def main() -> None:
    print("=" * 78)
    print("Hybrid A* offline benchmark (synthetic maps, no hardware)")
    print("=" * 78)

    # 1. Narrow corridor, also compared against the zero-heuristic baseline.
    sc = narrow_corridor()
    res_h, veh = run(sc)
    res_zero, _ = run(sc, PlannerConfig(use_heuristic=False))
    print(f"\n[{sc.name}] {sc.description}")
    for label, r in (("with heuristic", res_h), ("zero heuristic ", res_zero)):
        print(
            f"  {label}: success={r.success} cost={r.cost:.3f} "
            f"expansions={r.expansions} time={r.elapsed_ms:.1f} ms"
        )
    ok_col = check_collision_free(sc.grid, sc.resolution, veh,
                                  res_h.path_x, res_h.path_y, res_h.path_theta)
    ok_kin, kmax = check_kinematics(veh, res_h.path_x, res_h.path_y, res_h.path_theta)
    print(f"  collision-free (brute force): {ok_col}; "
          f"max |kappa| = {kmax:.4f} vs limit {veh.kappa_max:.4f} "
          f"(tol=1e-3 for chord sampling): {ok_kin}")

    # 2. Dead-end alley: forward-only fails, reverse succeeds.
    sc = reverse_uturn()
    res_fwd, veh = run(sc, PlannerConfig(allow_reverse=False))
    res_rev, _ = run(sc, PlannerConfig(allow_reverse=True))
    print(f"\n[{sc.name}] {sc.description}")
    print(f"  forward only : success={res_fwd.success} ({res_fwd.message}), "
          f"expansions={res_fwd.expansions}")
    n_rev = sum(1 for g in res_rev.path_gear if g < 0)
    print(f"  with reverse : success={res_rev.success} cost={res_rev.cost:.3f} "
          f"expansions={res_rev.expansions} time={res_rev.elapsed_ms:.1f} ms, "
          f"reverse samples={n_rev}/{len(res_rev.path_gear)}")
    ok_col = check_collision_free(sc.grid, sc.resolution, veh,
                                  res_rev.path_x, res_rev.path_y, res_rev.path_theta)
    ok_kin, kmax = check_kinematics(veh, res_rev.path_x, res_rev.path_y, res_rev.path_theta)
    print(f"  collision-free (brute force): {ok_col}; "
          f"max |kappa| = {kmax:.4f} vs limit {veh.kappa_max:.4f} "
          f"(tol=1e-3 for chord sampling): {ok_kin}")

    # 3. Unsolvable map.
    sc = unsolvable()
    res_no, _ = run(sc)
    print(f"\n[{sc.name}] {sc.description}")
    print(f"  success={res_no.success} message='{res_no.message}' "
          f"expansions={res_no.expansions} time={res_no.elapsed_ms:.1f} ms")

    print("\nDone.")


if __name__ == "__main__":
    main()
