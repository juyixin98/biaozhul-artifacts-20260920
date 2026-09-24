"""离线验收示例：正解生成目标 -> 逆解，覆盖全部要求场景。

不连真实硬件、不做可视化，只打印数值结果。运行：

    .venv/bin/python examples/run_demo.py
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kinematics import (  # noqa: E402
    ContinuousIKSolver,
    IKStatus,
    LinkParams,
    inverse_kinematics,
)
from app.synthetic import (  # noqa: E402
    both_elbow_targets,
    circle_path,
    extension_target,
    fold_target,
    path_with_unreachable_gap,
    smooth_fk_path,
    target_from_fk,
)

LINE = "-" * 72


def section(title: str) -> None:
    print("\n" + LINE)
    print(title)
    print(LINE)


def show(sol) -> None:
    if sol.joints is not None:
        print(
            f"  status={sol.status.value:16s} chosen={sol.chosen:11s} "
            f"q1={sol.joints[0]: .6f} q2={sol.joints[1]: .6f} "
            f"ee_err={sol.position_error:.2e} flipped={sol.flipped}"
        )
    else:
        gap = f" gap={sol.boundary_gap:.4f}" if sol.boundary_gap is not None else ""
        print(f"  status={sol.status.value:16s} joints=None{gap}")
        print(f"  note: {sol.note}")


def main() -> None:
    arm = LinkParams(l1=1.0, l2=1.0)

    # 1. 两支解析解 / 肘部翻转
    section("1. 同一目标 (1,1) 的两支解析解（肘部上/下翻转）")
    target = both_elbow_targets(arm)[0][2]
    for pref in (None, "elbow_up", "elbow_down"):
        show(inverse_kinematics(target, arm, preference=pref))

    # 2. 完全伸直
    section("2. 完全伸直奇异（q2=0，外边界 r=2）")
    show(inverse_kinematics(extension_target(math.pi / 4, arm), arm))

    # 3. 完全折叠
    section("3. 等长臂完全折叠奇异（末端原点）")
    show(inverse_kinematics(fold_target(arm), arm))

    # 4. 不可达（不裁剪坐标）
    section("4. 不可达目标（不裁剪坐标伪装成功）")
    for t in ((2.4, 0.0), (2.0, 2.0)):
        show(inverse_kinematics(t, arm))
    pn = LinkParams(l1=1.2, l2=0.8)
    show(inverse_kinematics((0.2, 0.0), pn))  # 内部空洞 r<0.4

    # 5. 关节限位冲突
    section("5. 关节限位冲突")
    limited = LinkParams(theta2_min=-math.pi, theta2_max=-0.2)  # 只允许肘上
    print("  自动选支（肘下被 q2<=-0.2 限位挡住）：")
    show(inverse_kinematics((1.0, 1.0), limited))
    print("  显式偏好被挡住的肘下支：")
    show(inverse_kinematics((1.0, 1.0), limited, preference="elbow_down"))
    blocked = LinkParams(theta2_min=0.3, theta2_max=0.5)
    print("  两支都撞限位：")
    show(inverse_kinematics((1.0, 1.0), blocked))

    # 6. 连续路径：FK 回放
    section("6. FK 回放平滑路径的连续选解")
    res = ContinuousIKSolver(arm).solve_path(smooth_fk_path(n=61))
    print(
        f"  solved={res.solved_count} failed={res.failed_count} "
        f"flips={res.flip_count} max_ee_err={res.max_position_error:.2e} "
        f"max_joint_step={res.max_joint_step:.4f}"
    )

    # 7. 圆路径（含近折叠奇异区的关节放大）
    section("7. 合成圆路径（经过近原点，观察近奇异关节放大）")
    circ = circle_path(n=72)
    res = ContinuousIKSolver(arm).solve_path(circ)
    r_min = min(math.hypot(x, y) for x, y in circ)
    print(
        f"  solved={res.solved_count} failed={res.failed_count} "
        f"flips={res.flip_count} singular={res.singular_count} "
        f"near_singular={res.near_singular_count} r_min={r_min:.3f}"
    )
    print(
        f"  max_ee_err={res.max_position_error:.2e} "
        f"mean_ee_err={res.mean_position_error:.2e} "
        f"max_joint_step={res.max_joint_step:.4f}"
    )
    # 更粗采样使路径点更贴近完全折叠点 r=0（但不越过），
    # 放大近奇异时的关节步长；选支仍应保持同一分支、零翻转。
    circ_coarse = circle_path(n=20)
    res_c = ContinuousIKSolver(arm).solve_path(circ_coarse)
    r_min_c = min(math.hypot(x, y) for x, y in circ_coarse)
    print(
        f"  粗采样(n=20) r_min={r_min_c:.3f}: "
        f"flips={res_c.flip_count} max_joint_step={res_c.max_joint_step:.4f} "
        f"max_ee_err={res_c.max_position_error:.2e}"
    )
    print("  （近折叠点附近关节步长被病态雅可比放大，但无肘部翻转=选支连续）")

    # 8. 路径中的不可达缺口
    section("8. 路径含不可达缺口（失败点不污染连续性基准）")
    res = ContinuousIKSolver(arm).solve_path(path_with_unreachable_gap(arm))
    for p in res.points:
        j = "None" if p.joints is None else f"({p.joints[0]:.3f},{p.joints[1]:.3f})"
        print(f"  pt{p.index}: {p.status.value:16s} joints={j}")

    # 9. 随机 FK 往返误差汇总
    section("9. 2000 个随机 FK 目标往返（精度摸底）")
    import random

    random.seed(42)
    worst = 0.0
    worst_near = 0.0
    for _ in range(2000):
        q1 = random.uniform(-math.pi, math.pi)
        q2 = random.uniform(-math.pi, math.pi)
        s = inverse_kinematics(target_from_fk(q1, q2, arm), arm)
        assert s.joints is not None
        worst = max(worst, s.position_error)
        if s.status == IKStatus.NEAR_SINGULAR:
            worst_near = max(worst_near, s.position_error)
    print(f"  全部 2000 点有解；最坏末端误差 = {worst:.3e}")
    print(f"  其中近奇异点最坏误差 = {worst_near:.3e}")
    print("\n全部示例运行完成。")


if __name__ == "__main__":
    main()
