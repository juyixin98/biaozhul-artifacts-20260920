"""六轴串联臂数值逆运动学：阻尼最小二乘 + 多初值 + 正解核验 + 多解选择。

失败分类（互斥）：
  SUCCESS                 正解核验通过的候选存在
  UNREACHABLE             目标腕点超出工作空间（2 连杆解析预检 + 求解交叉验证）
  SINGULAR_NO_CONVERGE    几何可达且非纯限位问题，但阻尼迭代在奇异附近不收敛
  LIMIT_CONFLICT          无约束解能达到目标，但无法落在关节限位内（含周期平移后）

硬性保证：任何 SUCCESS 都经过独立的正运动学核验；
核验不过的迭代结果只能作为诊断证据，绝不作为成功返回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from ..config import SolverConfig
from .angles import angular_distance, canonicalize_to_limits, nearest_equivalent_in_range, wrap_to_pi
from .reachability import wrist_reachability
from .robot_model import RobotModel, pose_error


class IKStatus(str, Enum):
    SUCCESS = "SUCCESS"
    UNREACHABLE = "UNREACHABLE"
    SINGULAR_NO_CONVERGE = "SINGULAR_NO_CONVERGE"
    LIMIT_CONFLICT = "LIMIT_CONFLICT"


@dataclass
class AttemptInfo:
    seed_index: int
    enforce_limits: bool
    converged: bool
    iterations: int
    position_error: float
    orientation_error: float
    sigma_min: float
    saturated_axes: list[int]
    terminated_reason: str


@dataclass
class Candidate:
    q: np.ndarray
    position_error: float
    orientation_error: float
    seed_index: int
    joint_distance: float
    raw_distance: float = 0.0

    def to_dict(self) -> dict:
        return {
            "joints": [float(x) for x in self.q],
            "joints_wrapped": [float(x) for x in wrap_to_pi(self.q)],
            "position_error": self.position_error,
            "orientation_error": self.orientation_error,
            "joint_distance_to_current": self.joint_distance,
            "raw_distance_to_current": self.raw_distance,
            "seed_index": self.seed_index,
        }


@dataclass
class IKResult:
    status: IKStatus
    q: np.ndarray | None = None
    position_error: float | None = None
    orientation_error: float | None = None
    candidates: list[Candidate] = field(default_factory=list)
    message: str = ""
    attempts: list[AttemptInfo] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)


def _analytic_wrist_seeds(robot: RobotModel, wc: np.ndarray) -> list[np.ndarray]:
    """由 2 连杆解析解构造接近目标的初值（肘上/肘下 × 方位两支 × 腕部构型）。

    平面 2 连杆解的是“肩平面内径向坐标 r 取正”的两支（肘 ±）。
    映射回基座标时还有 r 取负的方位翻转支：
      直接支：  q1 = atan2(y, x)，q2 = φ - ψ(rel)，       q3 = rel + δ
      翻转支：  q1' = q1 + π， q2' = ψ(-rel) - φ + π，    q3' = rel + δ
    （翻转支平面两矢量反向，等价于正径向下对侧肘支的几何；
      ψ(-rel)-φ+π 与 -φ+ψ(-rel) 相差 π，均为合法 r=-ρ 解。）
    """
    a2, a3 = float(robot.a[1]), float(robot.a[2])
    d1, d4 = float(robot.d[0]), float(robot.d[3])
    l3 = float(np.hypot(a3, d4))
    delta = float(np.arctan2(d4, a3))

    radius = float(np.hypot(wc[0], wc[1]))
    zp = float(wc[2]) - d1
    dist = float(np.hypot(radius, zp))
    cos_d = float(np.clip((dist**2 - a2**2 - l3**2) / (2.0 * a2 * l3), -1.0, 1.0))
    phi = float(np.arctan2(zp, radius))
    q1 = float(np.arctan2(wc[1], wc[0])) if radius > 1e-11 else 0.0

    wrist_patterns = [
        (0.0, 0.0, 0.0),
        (0.0, np.pi / 2, 0.0),
        (np.pi, np.pi / 2, 0.0),
        (0.0, -np.pi / 2, 0.0),
    ]

    base_solutions: list[tuple[float, float, float]] = []
    for rel in (np.arccos(cos_d), -np.arccos(cos_d)):
        psi = float(np.arctan2(l3 * np.sin(rel), a2 + l3 * np.cos(rel)))
        q2 = phi - psi
        q3 = rel + delta
        # 直接支：肩平面径向坐标取正
        base_solutions.append((q1, q2, q3))
        # 方位翻转支：r=-ρ；对侧肘支几何 ψ(-rel)
        psi_op = float(np.arctan2(l3 * np.sin(-rel), a2 + l3 * np.cos(-rel)))
        base_solutions.append((q1 + np.pi, psi_op - phi + np.pi, q3))

    seeds: list[np.ndarray] = []
    for bq1, bq2, bq3 in base_solutions:
        for q4, q5, q6 in wrist_patterns:
            seeds.append(np.array([bq1, bq2, bq3, q4, q5, q6], dtype=float))
    return seeds


def build_seed_set(
    robot: RobotModel,
    T_des: np.ndarray,
    current_q: np.ndarray | None,
    extra_seeds: list[np.ndarray] | None = None,
) -> list[np.ndarray]:
    """多初值集合：当前姿态、零位、解析腕部初值、限位中点与若干分散构型。"""
    low, high = robot.joint_lower, robot.joint_upper
    mid = 0.5 * (low + high)

    # 由目标腕点构造解析初值（目标 R 用于把末端沿 approach 反向投影到腕点）
    wc = T_des[:3, 3] - float(robot.d[5]) * T_des[:3, 2]
    seeds: list[np.ndarray] = []
    if current_q is not None:
        cq = canonicalize_to_limits(np.asarray(current_q, float), low, high, np.asarray(current_q, float))
        if cq is not None:
            seeds.append(cq)
    seeds.append(np.zeros(robot.n_joints))
    seeds.append(mid.copy())
    seeds.extend(_analytic_wrist_seeds(robot, wc))
    seeds.extend(
        [
            np.array([0.0, -0.6, 1.0, 0.0, 0.6, 0.0]),
            np.array([0.9, -0.4, 0.8, 1.2, 0.4, 1.0]),
            np.array([-0.9, -1.2, 0.4, -1.2, -0.4, -1.0]),
            np.array([1.6, -0.8, 1.4, 2.2, 0.9, 2.0]),
            np.array([-1.6, 0.2, -1.2, -2.2, -0.9, -2.0]),
        ]
    )
    if extra_seeds:
        for s in extra_seeds:
            c = canonicalize_to_limits(np.asarray(s, float), low, high)
            seeds.append(c if c is not None else np.asarray(s, float))

    # 去重（周期意义下）
    unique: list[np.ndarray] = []
    for s in seeds:
        s = np.asarray(s, dtype=float)
        if all(np.linalg.norm(angular_distance(s, u)) > 1e-3 for u in unique):
            unique.append(s)
    return unique


def _damped_step(J: np.ndarray, e: np.ndarray, damping: float) -> tuple[np.ndarray, np.ndarray]:
    """阻尼最小二乘下降方向。

    残差 e = target - f(q)，故 J = de/dq = -J_geo，
    下降步长 step = -J^T(JJ^T+λ²I)^{-1} e，可直接用于 q ← q + step。
    同时返回奇异值（诊断用）。
    """
    U, sigma, Vt = np.linalg.svd(J, full_matrices=False)
    inv = sigma / (sigma**2 + damping**2)
    step = -(Vt.T @ (inv * (U.T @ e)))
    return step, sigma


def run_dls(
    robot: RobotModel,
    cfg: SolverConfig,
    seed: np.ndarray,
    T_des: np.ndarray,
    enforce_limits: bool,
    seed_index: int,
) -> tuple[np.ndarray, AttemptInfo]:
    """从单个初值执行阻尼最小二乘迭代。

    enforce_limits=True 时每步把关节角夹回限位（用于求可落地解）；
    enforce_limits=False 完全不夹（用于区分“限位冲突”与“真奇异”）。
    """
    low, high = robot.joint_lower, robot.joint_upper
    q = seed.copy()
    damping = cfg.damping_initial
    best_err = np.inf
    stall = 0
    sigma_min = np.inf
    saturated: set[int] = set()
    reason = "max_iterations"

    T = robot.fk(q)
    for it in range(1, cfg.max_iterations + 1):
        e = pose_error(T, T_des)
        pos_err = float(np.linalg.norm(e[:3]))
        ori_err = float(np.linalg.norm(e[3:]))
        if pos_err <= cfg.position_tolerance and ori_err <= cfg.orientation_tolerance:
            reason = "converged"
            break

        J = robot.numerical_jacobian(q, T_des, cfg.fd_eps)
        step, sigma = _damped_step(J, e, damping)
        step_norm = float(np.linalg.norm(step))
        if step_norm > cfg.step_clip:
            step *= cfg.step_clip / step_norm

        if enforce_limits:
            q_unclipped = q + step
            q_next = np.clip(q_unclipped, low, high)
            for ax in range(robot.n_joints):
                if abs(q_unclipped[ax] - q_next[ax]) > 1e-9:
                    saturated.add(ax)
        else:
            q_next = q + step

        q = q_next
        T = robot.fk(q)
        sigma_min = min(sigma_min, float(sigma.min()))

        err_metric = pos_err + 0.6 * ori_err
        if err_metric < best_err - 1e-9:
            best_err = err_metric
            stall = 0
            damping = max(cfg.damping_initial, damping * 0.7)
        else:
            stall += 1
            damping = min(cfg.damping_max, damping * 1.5)
            if stall >= cfg.singular_stall_iterations and sigma_min < 0.05:
                reason = "stalled_near_singular"
                break
    else:
        it = cfg.max_iterations

    e = pose_error(T, T_des)
    converged = reason == "converged"
    info = AttemptInfo(
        seed_index=seed_index,
        enforce_limits=enforce_limits,
        converged=converged,
        iterations=it,
        position_error=float(np.linalg.norm(e[:3])),
        orientation_error=float(np.linalg.norm(e[3:])),
        sigma_min=sigma_min,
        saturated_axes=sorted(saturated),
        terminated_reason=reason,
    )
    return q, info


def _verify(robot: RobotModel, q: np.ndarray, T_des: np.ndarray, cfg: SolverConfig) -> tuple[bool, float, float]:
    """独立正解核验：用候选关节角重新做 FK，与目标逐维比较。"""
    T = robot.fk(q)
    e = pose_error(T, T_des)
    pe, oe = float(np.linalg.norm(e[:3])), float(np.linalg.norm(e[3:]))
    ok = pe <= cfg.verify_position_tolerance and oe <= cfg.verify_orientation_tolerance
    return ok, pe, oe


def _in_limit_representatives(q: np.ndarray, low: np.ndarray, high: np.ndarray) -> list[np.ndarray]:
    """枚举 q 在限位盒内的所有 2π 周期代表组合。

    宽度 < 2π 的轴至多一个代表；J6 这种 ±2π（宽 4π）的轴可能有两个，
    因此返回笛卡尔积（本机器人最多 2 个）。任何轴无代表角则返回空。
    """
    per_axis: list[list[float]] = []
    for i, v in enumerate(q):
        lo_i, hi_i = float(low[i]), float(high[i])
        vv = float(v)
        reps = []
        k = int(np.floor((lo_i - vv) / (2.0 * np.pi)))
        while True:
            cand = vv + k * 2.0 * np.pi
            if cand > hi_i + 1e-12:
                break
            if cand >= lo_i - 1e-12:
                reps.append(float(cand))
            k += 1
        if not reps:
            return []
        per_axis.append(sorted(set(np.round(reps, 12))))

    import itertools

    combos = [np.array(vals, dtype=float) for vals in itertools.product(*per_axis)]
    out: list[np.ndarray] = []
    for c in combos:
        # 注意：这里不能用周期距离去重——5.5 与 -0.783 正运动学等价，
        # 但作为关节值是两个不同代表角，多解择优需要同时保留它们。
        if all(float(np.linalg.norm(c - u)) > 1e-9 for u in out):
            out.append(c)
    return out


def _equivalent_branches(q: np.ndarray) -> list[np.ndarray]:
    """展开球形腕的 8 组运动学等价解（J6 周期平移另由 canonicalize 处理）。

    球形腕存在两组符号等价：
      flip A：(q4+π, π-q5, q6+π)
      flip B：(q4,   -q5,  q6+π)
    以及两者复合。不同分支在限位盒中的可行性不同，
    无约束解越限时，必须把这些分支也试一遍才能断定“限位冲突”。
    """
    q = np.asarray(q, dtype=float)
    q4, q5, q6 = q[3], q[4], q[5]
    variants = [
        (q4, q5, q6),
        (q4 + np.pi, np.pi - q5, q6 + np.pi),
        (q4, -q5, q6 + np.pi),
        (q4 + np.pi, q5 - np.pi, q6),
    ]
    out = [q.copy()]
    for a, b, c in variants[1:]:
        qn = q.copy()
        qn[3], qn[4], qn[5] = a, b, c
        out.append(qn)
    return out


def solve_ik(
    robot: RobotModel,
    cfg: SolverConfig,
    T_des: np.ndarray,
    current_q: np.ndarray | None = None,
    extra_seeds: list[np.ndarray] | None = None,
) -> IKResult:
    """多初值阻尼逆解主入口（两阶段）。

    阶段 A：多初值**无约束**阻尼迭代，找出目标在无限位意义下的真实解；
    阶段 B：把每个核验过的无约束解展开为球形腕等价分支，周期平移进限位后
            作为热启动跑**受约束**阻尼迭代。
            - 有任何分支在限位内收敛并通过正解核验 → SUCCESS（按周期距离择优）
            - 真实解全部只能落在限位外             → LIMIT_CONFLICT
            - 阶段 A 就不收敛：
                预检证明超出臂展                     → UNREACHABLE
                否则                                 → SINGULAR_NO_CONVERGE
    """
    T_des = np.asarray(T_des, dtype=float)
    low, high = robot.joint_lower, robot.joint_upper

    # 参考姿态用于多解择优（未提供 current 时以零位为参考）
    reference = np.asarray(current_q, float) if current_q is not None else np.zeros(robot.n_joints)

    # ---- 1) 球形腕可达性解析预检 ----
    wc_des = T_des[:3, 3] - float(robot.d[5]) * T_des[:3, 2]
    pre = wrist_reachability(robot, wc_des)

    base_seeds = build_seed_set(robot, T_des, current_q, extra_seeds)
    attempts: list[AttemptInfo] = []
    best_sigma = np.inf

    # ---- 2) 阶段 A：无约束多初值 ----
    free_solutions: list[tuple[np.ndarray, float, float, int]] = []
    seen = np.zeros(0)
    free_sol_list: list[np.ndarray] = []
    for idx, seed in enumerate(base_seeds):
        q_star, info = run_dls(robot, cfg, seed, T_des, False, idx)
        attempts.append(info)
        best_sigma = min(best_sigma, info.sigma_min)
        if not info.converged:
            continue
        ok, pe, oe = _verify(robot, q_star, T_des, cfg)
        if not ok:
            continue
        # 去重（周期意义下同解）
        if any(float(np.linalg.norm(angular_distance(q_star, u))) < 1e-4 for u in free_sol_list):
            continue
        free_sol_list.append(q_star)
        free_solutions.append((q_star, pe, oe, idx))

    # ---- 3) 阶段 B：等价分支 + 周期代表 + 热启动约束求解 ----
    verified: list[Candidate] = []
    hot_seeds: list[np.ndarray] = []
    for q_free, _, _, _ in free_solutions:
        for branch in _equivalent_branches(q_free):
            hot_seeds.extend(_in_limit_representatives(branch, low, high))
    # 精确值去重（周期等价但关节值不同的代表角必须分别保留，供择优）
    uniq_hot: list[np.ndarray] = []
    for s in hot_seeds:
        if all(float(np.linalg.norm(s - u)) > 1e-6 for u in uniq_hot):
            uniq_hot.append(s)

    for h_idx, seed in enumerate(uniq_hot):
        q_star, info = run_dls(robot, cfg, seed, T_des, True, h_idx)
        attempts.append(info)
        best_sigma = min(best_sigma, info.sigma_min)
        if not info.converged:
            continue
        # 解出来后同样枚举所有限位内周期代表（保留离参考近的，如 q6≈+5.5 而非 -0.78）
        for qc in _in_limit_representatives(q_star, low, high):
            ok, pe, oe = _verify(robot, qc, T_des, cfg)
            if not ok:
                continue
            if np.any(qc < low - 1e-8) or np.any(qc > high + 1e-8):
                continue
            dist = float(np.linalg.norm(angular_distance(qc, reference)))
            raw_dist = float(np.linalg.norm(qc - reference))
            if all(float(np.linalg.norm(qc - c.q)) > 1e-5 for c in verified):
                verified.append(
                    Candidate(qc, pe, oe, len(base_seeds) + h_idx, dist, raw_dist)
                )

    total_seeds = len(base_seeds) + len(uniq_hot)

    # ---- 4) 多解择优 ----
    # 主键：周期关节距离（真正的运动代价）。
    # 周期等价解（如 q6=5.52 与 -0.76）前 5 轴会因独立 DLS 路径存在
    # 1e-4 量级差异，周期距离不会严格相等，因此先取最优周期距离的小邻域
    # (1e-3 rad)，再在邻域内用“原始数值距离”选与当前姿态同周期支的代表，
    # 避免不必要的整圈回转；最后以核验误差兜底。
    if verified:
        d_min = min(c.joint_distance for c in verified)
        pool = [c for c in verified if c.joint_distance <= d_min + 1e-3]
        pool.sort(key=lambda c: (c.raw_distance, c.position_error + c.orientation_error))
        best = pool[0]
        # 响应中的候选列表仍按周期距离排序展示
        verified.sort(key=lambda c: (c.joint_distance, c.raw_distance))
        return IKResult(
            status=IKStatus.SUCCESS,
            q=best.q,
            position_error=best.position_error,
            orientation_error=best.orientation_error,
            candidates=verified,
            message=f"{len(verified)} 个候选通过正解核验，已按离当前姿态的周期关节距离择优",
            attempts=attempts,
            diagnostics={
                "seed_count": total_seeds,
                "unconstrained_solutions": len(free_solutions),
                "precheck": pre.reason or "reachable",
                "wrist_max_reach": pre.max_reach,
                "wrist_requested_radius": pre.requested_radius,
                "smallest_singular_value": best_sigma,
            },
        )

    # ---- 5) 失败三分类 ----
    if not free_solutions:
        if not pre.reachable:
            return IKResult(
                status=IKStatus.UNREACHABLE,
                message=(
                    f"目标腕点距离肩轴 {pre.requested_radius:.4f} m，"
                    f"超出最大臂展 {pre.max_reach:.4f} m，且阻尼迭代无任何初值收敛"
                ),
                attempts=attempts,
                diagnostics={
                    "seed_count": total_seeds,
                    "unconstrained_solutions": 0,
                    "precheck": "out_of_workspace",
                    "wrist_max_reach": pre.max_reach,
                    "wrist_requested_radius": pre.requested_radius,
                    "smallest_singular_value": best_sigma,
                },
            )
        return IKResult(
            status=IKStatus.SINGULAR_NO_CONVERGE,
            message=(
                f"目标在工作空间内（预检腕距 {pre.requested_radius:.4f} m ≤ 最大臂展 "
                f"{pre.max_reach:.4f} m）且非纯限位问题，但 {len(base_seeds)} 组初值的阻尼迭代"
                f"均未收敛（最小雅可比奇异值 {best_sigma:.3e}，判定处于奇异附近/病态区域）"
            ),
            attempts=attempts,
            diagnostics={
                "seed_count": total_seeds,
                "unconstrained_solutions": 0,
                "precheck": pre.reason or "reachable",
                "wrist_max_reach": pre.max_reach,
                "wrist_requested_radius": pre.requested_radius,
                "smallest_singular_value": best_sigma,
            },
        )

    # 存在无限位解、但所有等价分支周期平移后均无法在限位内核验 → 限位冲突。
    # 诊断：为每组去重后的无约束解找“尽量贴限位”的翻腕分支代表，
    # 统计越限轴数与越限量；取越限最少的那组报告 offending_joints。
    # 注意耦合冲突：每个轴可能在某组解上可行，但不存在同时满足所有轴的组合。
    q_free = free_solutions[0][0]
    per_solution: list[dict] = []
    best_score: tuple[int, float, list[int], np.ndarray] | None = None
    for sol, _, _, _ in free_solutions:
        # 每个轴独立取“最贴近限位区间”的周期代表（可能仍越限），再统计
        rq = np.zeros(robot.n_joints)
        for i in range(robot.n_joints):
            v = float(sol[i])
            rep = nearest_equivalent_in_range(v, float(low[i]), float(high[i]))
            rq[i] = rep if rep is not None else v
        viol = [
            i + 1
            for i in range(robot.n_joints)
            if rq[i] < low[i] - 1e-8 or rq[i] > high[i] + 1e-8
        ]
        excess = float(
            sum(
                max(0.0, float(low[i] - rq[i])) + max(0.0, float(rq[i] - high[i]))
                for i in range(robot.n_joints)
            )
        )
        per_solution.append(
            {
                "joints": [float(x) for x in rq],
                "violating_axes": viol,
                "violation_count": len(viol),
                "total_excess_rad": excess,
            }
        )
        if best_score is None or (len(viol), excess) < (best_score[0], best_score[1]):
            best_score = (len(viol), excess, viol, rq)
    offending = best_score[2] if best_score else []
    return IKResult(
        status=IKStatus.LIMIT_CONFLICT,
        message=(
            f"目标运动学可达，但 {len(free_solutions)} 组无约束解（共展开 "
            f"{len(uniq_hot)} 个限位内周期等价热启动）均无法在关节限位内核验通过"
            f"（已考虑角度 2π 周期等价与球形腕翻腕分支）；"
            f"最接近可行的解仍在关节 {offending} 越限"
            + ("（可能为多轴耦合冲突）" if best_score and best_score[0] >= 1 and len(free_solutions) > 1 else "")
        ),
        attempts=attempts,
        diagnostics={
            "seed_count": total_seeds,
            "unconstrained_solutions": len(free_solutions),
            "precheck": pre.reason or "reachable",
            "wrist_max_reach": pre.max_reach,
            "wrist_requested_radius": pre.requested_radius,
            "smallest_singular_value": best_sigma,
            "free_solution_example": [float(x) for x in q_free],
            "offending_joints": offending,
            "per_solution_limits": per_solution[:8],
        },
    )
