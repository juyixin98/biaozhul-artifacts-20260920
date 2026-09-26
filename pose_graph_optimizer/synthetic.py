"""合成轨迹与传感器数据生成（不连接任何硬件）。

所有数据均由已知“真值”轨迹程序化生成：

- 真值轨迹：由局部坐标系下的相对运动指令积分得到（正方形 / 圆形 / 直线）；
- 里程计边：真值相对位姿 + 高斯噪声，初始猜测直接由噪声里程计积分，
  因此长轨迹会自然累积漂移；
- 回环边：对指定节点对测量真值相对位姿（+ 小噪声）；
- 错误回环：在测量中人为注入大的平移/角度偏差，用于检验鲁棒核；
- 不连通场景：两条独立链，用于检验结构诊断。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .graph import PoseGraph, make_information_matrix
from .kernels import Kernel
from .se2 import compose_pose, invert_pose, wrap_angle


@dataclass(frozen=True)
class NoiseModel:
    """测量噪声标准差与对应信息矩阵。"""

    sigma_xy: float = 0.05
    sigma_theta: float = np.deg2rad(2.0)

    def info(self) -> np.ndarray:
        return make_information_matrix(self.sigma_xy, self.sigma_theta)


def poses_from_motions(
    commands: list[tuple[float, float, float]],
) -> np.ndarray:
    """从局部运动指令 ``(dx, dy, dtheta)`` 积分出全局真值位姿序列。"""
    poses = [np.array([0.0, 0.0, 0.0])]
    cur = poses[0].copy()
    for dx, dy, dth in commands:
        cur = compose_pose(cur, np.array([dx, dy, dth], dtype=float))
        poses.append(cur.copy())
    return np.array(poses)


def square_trajectory(side_length: float = 4.0, per_side: int = 5) -> np.ndarray:
    """闭合正方形轨迹，共 ``4*per_side + 1`` 个节点（首尾位姿重合）。"""
    step = side_length / per_side
    commands: list[tuple[float, float, float]] = []
    for side in range(4):
        commands.extend([(step, 0.0, 0.0)] * per_side)
        if side < 3:
            commands.append((0.0, 0.0, np.pi / 2.0))
        else:
            commands.append((0.0, 0.0, np.pi / 2.0))
    return poses_from_motions(commands)


def circle_trajectory(
    radius: float = 5.0, steps: int = 40, turns: float = 1.0
) -> np.ndarray:
    """圆形轨迹（朝向为切线方向），位姿角累计可超过 ±π，用于跨 pi 检验。"""
    total_angle = 2.0 * np.pi * turns
    dth = total_angle / steps
    # 弦长作为局部前进步
    step = 2.0 * radius * np.sin(dth / 2.0)
    commands = [(step, 0.0, dth)] * steps
    return poses_from_motions(commands)


def line_trajectory(length: float = 8.0, steps: int = 10) -> np.ndarray:
    """直线轨迹。"""
    step = length / steps
    return poses_from_motions([(step, 0.0, 0.0)] * steps)


def _noisy_measurement(
    truth_rel: np.ndarray, rng: np.random.Generator, noise: NoiseModel
) -> np.ndarray:
    m = truth_rel.copy()
    m[0] += rng.normal(0.0, noise.sigma_xy)
    m[1] += rng.normal(0.0, noise.sigma_xy)
    m[2] = wrap_angle(m[2] + rng.normal(0.0, noise.sigma_theta))
    return m


def _truth_relative(poses: np.ndarray, i: int, j: int) -> np.ndarray:
    return compose_pose(invert_pose(poses[i]), poses[j])


def build_graph_from_poses(
    truth_poses: np.ndarray,
    noise: NoiseModel | None = None,
    loop_pairs: list[tuple[int, int]] | None = None,
    false_loops: list[dict] | None = None,
    seed: int = 0,
    fixed_id: int | None = 0,
    use_odometry_initial_guess: bool = True,
) -> tuple[PoseGraph, np.ndarray]:
    """由真值轨迹构造位姿图。

    Args:
        truth_poses: ``(N, 3)`` 真值位姿。
        loop_pairs: 正确回环节点对 ``(i, j)``。
        false_loops: 错误回环列表，每项形如
            ``{"pair": (i, j), "offset": [dx, dy, dtheta], "info_scale": s,
               "kernel": Kernel(...)}``，offset 注入到真值相对测量上。
        seed: 随机种子，保证可复现。
        fixed_id: 固定的节点 id；``None`` 表示不固定（用于诊断测试）。
        use_odometry_initial_guess: 为 True 时初始猜测用噪声里程计积分
            （模拟真实漂移），否则在真值上加噪。

    Returns:
        (位姿图, 真值位姿数组)
    """
    noise = noise or NoiseModel()
    rng = np.random.default_rng(seed)
    n = len(truth_poses)
    graph = PoseGraph()
    info = noise.info()

    # 先生成噪声里程计测量与初始猜测
    odom_measurements: list[np.ndarray] = []
    initial = np.zeros_like(truth_poses)
    initial[0] = truth_poses[0].copy()
    for k in range(1, n):
        truth_rel = _truth_relative(truth_poses, k - 1, k)
        z = _noisy_measurement(truth_rel, rng, noise)
        odom_measurements.append(z)
        initial[k] = compose_pose(initial[k - 1], z)

    if not use_odometry_initial_guess:
        initial = truth_poses.copy()
        initial[:, 0] += rng.normal(0.0, 0.1, n)
        initial[:, 1] += rng.normal(0.0, 0.1, n)
        initial[:, 2] = np.array(
            [wrap_angle(t + rng.normal(0.0, 0.05)) for t in initial[:, 2]]
        )

    for k in range(n):
        graph.add_node(
            k, initial[k], fixed=(fixed_id is not None and k == fixed_id)
        )
    for k, z in enumerate(odom_measurements):
        graph.add_edge(k, k + 1, z, info, Kernel("none"))

    for i, j in loop_pairs or []:
        z = _noisy_measurement(_truth_relative(truth_poses, i, j), rng, noise)
        graph.add_edge(i, j, z, info, Kernel("none"))

    for spec in false_loops or []:
        fi, fj = spec["pair"]
        offset = np.asarray(spec.get("offset", [1.0, 1.0, 0.3]), dtype=float)
        z = _truth_relative(truth_poses, fi, fj) + offset
        z[2] = wrap_angle(z[2])
        scale = float(spec.get("info_scale", 1.0))
        kernel = spec.get("kernel", Kernel("huber", 1.0))
        graph.add_edge(fi, fj, z, scale * info, kernel)

    return graph, truth_poses


def build_disconnected_graph(
    seed: int = 1,
) -> tuple[PoseGraph, np.ndarray]:
    """构造不连通图：两条独立链，只固定第一条链的节点 0。

    第二条链（节点 5..9）没有任何边与主分量相连，也没有固定节点，
    其整体 3 个自由度不可观——优化器应在求解前诊断并报错。
    """
    chain_a = line_trajectory(length=4.0, steps=4)  # 5 个节点
    chain_b_poses = line_trajectory(length=4.0, steps=4)
    # 把第二条链平移到别处
    chain_b_poses = chain_b_poses.copy()
    chain_b_poses[:, 0] += 10.0

    noise = NoiseModel()
    rng = np.random.default_rng(seed)
    graph = PoseGraph()
    info = noise.info()

    all_truth = np.vstack([chain_a, chain_b_poses])
    for k, pose in enumerate(all_truth):
        graph.add_node(k, pose + np.array([0.05 * k, 0.0, 0.0]), fixed=(k == 0))

    for chain, offset in ((chain_a, 0), (chain_b_poses, 5)):
        for k in range(1, len(chain)):
            truth_rel = _truth_relative(
                np.vstack([chain_a, chain_b_poses]), offset + k - 1, offset + k
            )
            z = _noisy_measurement(truth_rel, rng, noise)
            graph.add_edge(offset + k - 1, offset + k, z, info, Kernel("none"))
    return graph, all_truth


def standard_scenarios(seed: int = 42) -> dict[str, dict]:
    """返回 README/示例使用的标准场景描述（供 CLI 与 JSON 样例生成复用）。"""
    return {
        "square": {
            "description": "正方形闭环轨迹（25 个节点），含 1 条正确回环和 1 条错误回环（Tukey 核硬拒绝）",
            "builder": lambda: build_graph_from_poses(
                square_trajectory(side_length=4.0, per_side=5),
                loop_pairs=[(0, 24)],
                false_loops=[
                    {
                        "pair": (3, 15),
                        "offset": [1.5, -1.0, 0.4],
                        "kernel": Kernel("tukey", 3.0),
                    }
                ],
                seed=seed,
            ),
        },
        "circle_wrap": {
            "description": "整圆轨迹（朝向跨越 ±π），回环检验角度跨 pi，Tukey 核硬拒绝错误回环",
            "builder": lambda: build_graph_from_poses(
                circle_trajectory(radius=5.0, steps=40, turns=1.0),
                loop_pairs=[(0, 40)],
                false_loops=[
                    {
                        "pair": (8, 32),
                        "offset": [2.0, 0.5, 0.0],
                        "kernel": Kernel("tukey", 4.0),
                    }
                ],
                seed=seed + 1,
            ),
        },
        "disconnected": {
            "description": "两条不连通链（只固定主分量），用于图不连通诊断",
            "builder": lambda: build_disconnected_graph(seed=seed + 2),
        },
        "line_no_loop": {
            "description": "无回环直线轨迹（开环里程计），基准场景",
            "builder": lambda: build_graph_from_poses(
                line_trajectory(length=8.0, steps=10),
                loop_pairs=[],
                false_loops=[],
                seed=seed + 3,
            ),
        },
    }
