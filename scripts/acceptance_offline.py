#!/usr/bin/env python3
"""离线验收脚本：不启动 HTTP 服务，直接验证计算链路全部关键场景。

  python scripts/acceptance_offline.py

逐项打印并断言：
  1. 已知正解往返（多随机构型，FK 独立核验）
  2. 伸直奇异位姿
  3. 超工作空间目标 → UNREACHABLE
  4. 限位冲突 → LIMIT_CONFLICT（含周期等价展开）
  5. 初值变化影响吸引盆/结果
  6. 周期距离选解（q6 正/负两支）
  7. 角度周期距离 ≠ 普通差值
  8. 有限预算下 SINGULAR_NO_CONVERGE，恢复预算后成功
"""

from __future__ import annotations

import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from app.config import load_solver_config
from app.core.angles import angular_distance
from app.core.ik import IKStatus, solve_ik
from app.core.robot_model import RobotModel, pose_error


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    robot = RobotModel.from_params()
    cfg = load_solver_config()
    all_ok = True

    # 1
    rng = np.random.default_rng(20260923)
    n_ok = 0
    for _ in range(10):
        q = robot.joint_lower + rng.random(6) * (robot.joint_upper - robot.joint_lower)
        T = robot.fk(q)
        r = solve_ik(robot, cfg, T, current_q=np.zeros(6))
        if r.status == IKStatus.SUCCESS:
            e = pose_error(robot.fk(r.q), T)
            if np.linalg.norm(e[:3]) < 1e-4 and np.linalg.norm(e[3:]) < 1e-4:
                n_ok += 1
    all_ok &= check("已知正解往返 + 独立正解核验", n_ok == 10, f"{n_ok}/10")

    # 2
    delta = np.arctan2(robot.d[3], robot.a[2])
    Ts = robot.fk(np.array([0.4, 0.0, delta, 0.0, 0.7, 0.0]))
    rs = solve_ik(robot, cfg, Ts, current_q=np.zeros(6))
    ok2 = rs.status in (IKStatus.SUCCESS, IKStatus.SINGULAR_NO_CONVERGE)
    if rs.status == IKStatus.SUCCESS:
        e = pose_error(robot.fk(rs.q), Ts)
        ok2 &= np.linalg.norm(e[:3]) < 1e-4
    all_ok &= check("伸直奇异位姿", ok2, rs.status.value)

    # 3
    Tfar = np.eye(4)
    Tfar[0, 3] = 10.0
    rf = solve_ik(robot, cfg, Tfar)
    all_ok &= check("超范围目标 → UNREACHABLE", rf.status == IKStatus.UNREACHABLE,
                    f"腕距 {rf.diagnostics['wrist_requested_radius']:.3f} > {rf.diagnostics['wrist_max_reach']:.3f}")

    # 4
    ql = np.array([3.1, -0.2, 0.4, 0.1, 0.2, 0.1])
    rl = solve_ik(robot, cfg, robot.fk(ql))
    all_ok &= check("限位冲突 → LIMIT_CONFLICT", rl.status == IKStatus.LIMIT_CONFLICT
                    and 1 in rl.diagnostics.get("offending_joints", []),
                    f"offending={rl.diagnostics.get('offending_joints')}")

    # 5
    q5 = np.array([0.9, -0.9, 1.2, -0.8, 0.8, 1.5])
    r5 = solve_ik(robot, cfg, robot.fk(q5), current_q=q5, extra_seeds=[q5])
    d5 = float(np.linalg.norm(angular_distance(r5.q, q5))) if r5.q is not None else 9.9
    all_ok &= check("初值变化：给定生成构型作初值收敛到该构型",
                    r5.status == IKStatus.SUCCESS and d5 < 0.05, f"距离 {d5:.4f}")

    # 6
    q6 = np.array([0.6, -0.4, 0.8, 0.5, 0.3, 5.5])
    T6 = robot.fk(q6)
    ra = solve_ik(robot, cfg, T6, current_q=np.array([0.5, -0.3, 0.7, 0.4, 0.2, -0.78]))
    rb = solve_ik(robot, cfg, T6, current_q=np.array([0.5, -0.3, 0.7, 0.4, 0.2, 5.5]))
    ok6 = ra.q[5] < 0 and rb.q[5] > 5.0
    all_ok &= check("周期距离选解：q6 随当前姿态取负/正支", ok6,
                    f"q6={ra.q[5]:+.3f} / {rb.q[5]:+.3f}")

    # 7
    plain = abs(5.5 - (-0.7832))
    periodic = float(angular_distance(5.5, -0.7832))
    all_ok &= check("角度周期距离 ≠ 普通差值", plain > 6.0 and periodic < 0.01,
                    f"普通差 {plain:.3f} vs 周期 {periodic:.4f}")

    # 8
    q8 = np.array([-1.4, -1.1, 1.7, 2.3, -1.1, 4.2])
    tight = dataclasses.replace(cfg, max_iterations=3, singular_stall_iterations=10)
    rt = solve_ik(robot, tight, robot.fk(q8), current_q=np.zeros(6))
    rf2 = solve_ik(robot, cfg, robot.fk(q8), current_q=np.zeros(6))
    all_ok &= check("预算受限时 SINGULAR_NO_CONVERGE、恢复后成功",
                    rt.status == IKStatus.SINGULAR_NO_CONVERGE and rf2.status == IKStatus.SUCCESS)

    print("\n结果：", "全部通过 ✅" if all_ok else "存在失败 ❌")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
