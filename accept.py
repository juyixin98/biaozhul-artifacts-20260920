"""Command-line acceptance walk-through (no HTTP needed).

Runs the four required scenarios and prints evidence:
  1. constraint activation (a_max saturation, v_max respected)
  2. abrupt reference change
  3. infeasible initial state -> conservative fallback -> recovery
  4. solver failure -> explicit conservative fallback with reason

Run:  python accept.py
"""

from __future__ import annotations

import numpy as np

from mpc.config import MPCConfig
from mpc.disturbance import DisturbanceSpec
from mpc.mpc import MPCController
from mpc.simulation import constant_reference, rollout, step_reference


def banner(t: str) -> None:
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def main() -> int:
    cfg = MPCConfig()
    c = MPCController(cfg)
    failures = []

    # ---- 1. constraint activation ----------------------------------------
    banner("1. CONSTRAINT ACTIVATION (x0=[0,0], p_ref=10)")
    ref = constant_reference((10.0, 0.0), cfg.horizon + 1)
    r = c.solve([0.0, 0.0], ref)
    u = np.array(r.control_sequence)
    v = np.array(r.predicted_states)[:, 1]
    print(f"status={r.status}  first 8 u = {np.round(u[:8], 4).tolist()}")
    print(f"max|u|={np.max(np.abs(u)):.6f} (bound {cfg.a_max}); "
          f"saturated steps={int(np.sum(np.isclose(u, cfg.a_max, atol=1e-4)))}")
    print(f"max|v| over horizon={np.max(np.abs(v)):.6f} (bound {cfg.v_max})")
    print(f"residuals={r.residuals}")
    ok = (r.status == "ok"
          and np.max(np.abs(u)) <= cfg.a_max + 1e-5
          and np.max(np.abs(v)) <= cfg.v_max + 1e-5
          and int(np.sum(np.isclose(u, cfg.a_max, atol=1e-4))) >= 3)
    print("PASS" if ok else "FAIL")
    failures.append(not ok)

    # ---- 2. reference step change ----------------------------------------
    banner("2. ABRUPT REFERENCE CHANGE 0 -> 1.5 at step 20 (sine disturbance)")
    refs = step_reference((0, 0), (1.5, 0), 20, 60, cfg.horizon)
    out = rollout(c, [0.0, 0.0], refs, 60,
                  DisturbanceSpec(kind="sine", amplitude=0.08,
                                  frequency=0.2))
    for k in (19, 20, 21, 30, 45, 59):
        s = out.steps[k]
        print(f"k={s.k:2d} t={s.t:4.1f} p={s.state[0]:8.4f} v={s.state[1]:7.4f} "
              f"u={s.control:7.4f} status={s.status}")
    print(f"fallback_count={out.fallback_count}  final={out.final_state}")
    print(f"max velocity violation={out.max_velocity_violation:.2e}, "
          f"max acceleration violation={out.max_acceleration_violation:.2e}")
    ok = (out.fallback_count == 0
          and out.max_velocity_violation <= 1e-6
          and out.max_acceleration_violation <= 1e-6)
    print("PASS" if ok else "FAIL")
    failures.append(not ok)

    # ---- 3. infeasible initial state -------------------------------------
    banner("3. INFEASIBLE INITIAL STATE v0=2.45 > v_max=2.0")
    x = np.array([0.0, 2.45])
    uprev = 0.0
    for k in range(6):
        rr = c.solve(x, constant_reference((0.0, 0.0), cfg.horizon + 1),
                     u_prev=uprev)
        print(f"k={k} state={np.round(x,4).tolist()} -> {rr.status:8s} "
              f"reason={rr.reason.value:26s} u={rr.control:7.4f}")
        assert abs(rr.control) <= cfg.a_max, "fallback violated a_max"
        x = c.model.step(x, rr.control)
        uprev = rr.control
    ok = abs(x[1]) <= cfg.v_max + 1e-6
    print(f"recovered velocity={x[1]:.4f}  ->  {'PASS' if ok else 'FAIL'}")
    failures.append(not ok)

    # ---- 4. solver failure injection -------------------------------------
    banner("4. SOLVER FAILURE -> CONSERVATIVE FALLBACK (no stale control)")
    good = c.solve([0.0, 0.0], ref)
    print(f"(previous verified plan had u0={good.control:.4f})")
    for mode in ("timeout", "infeasible", "error", "nonoptimal"):
        c.force_fail = mode
        moving = np.array([1.0, 1.5])
        rr = c.solve(moving, ref, u_prev=good.control)
        print(f"{mode:11s} -> status={rr.status:8s} reason="
              f"{rr.reason.value:22s} fallback u={rr.control:7.4f} "
              f"(expected braking {-cfg.k_brake * moving[1]:.4f}) "
              f"detail={rr.reason_detail}")
        assert rr.fallback and rr.control_sequence == []
        assert abs(rr.control - (-cfg.k_brake * moving[1])) < 1e-12
    c.force_fail = None
    print("PASS")

    banner(f"SUMMARY: {'ALL SCENARIOS PASSED' if not any(failures) else 'FAILURES'}")
    return 1 if any(failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
