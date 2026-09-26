"""合成轨迹与传感器数据生成器（纯数值，无硬件、无外部依赖）。

场景（模拟一台平面三关节臂 + 固定测距传感器）：
    base -> link1 -> link2 -> tool      （随时间变化的关节链）
    base -> sensor                     （静态安装的传感器）

所有数据由解析式合成：关节角随时间正弦变化，可直接手算复核。
"""

from __future__ import annotations

import numpy as np


def _z_pose(angle: float, translation: tuple[float, float, float]) -> dict:
    return {
        "translation": list(translation),
        "rotation": {"axis_angle": {"axis": [0, 0, 1], "angle": angle}},
    }


def build_demo_request() -> dict:
    """构造一份覆盖正常查询与各类报错的完整请求。

    几何（均为绕 z 轴旋转、沿 z 轴平移，便于手算）：
    - T_base_link1：绕 z 角 theta1(t)，平移 (0, 0, 0.10)
    - T_link1_link2：绕 z 角 theta2(t)，平移 (0, 0, 0.20)
    - T_link2_tool：绕 z 角 0，平移 (0, 0, 0.15)（静态）
    - T_base_sensor：静态，平移 (0.5, 0, 0.3)
    其中 theta1(t) = 0.1 t（rad），theta2(t) 在 t∈[0,4] 恒为 0，
    t∈[6,10] 恒为 pi/2 —— 4s 与 6s 之间故意留出 2s 的数据缺口。
    """
    times_link1 = [round(0.5 * i, 3) for i in range(21)]  # 0 .. 10，步长 0.5
    kf_link1 = [
        {"t": t, **_z_pose(angle=0.1 * t, translation=(0.0, 0.0, 0.10))} for t in times_link1
    ]

    # link2：正常时段每 0.5s 一帧（常量姿态），仅 (4,6) 之间断流，
    # 这样正常间隔 0.5 <= max_gap(0.6) 可插值，2s 缺口必然报错。
    kf_link2 = [
        {"t": round(0.5 * i, 3), **_z_pose(0.0, (0.0, 0.0, 0.20))}
        for i in range(9)  # 0.0 .. 4.0
    ]
    kf_link2 += [
        {"t": round(6.0 + 0.5 * i, 3), **_z_pose(np.pi / 2, (0.0, 0.0, 0.20))}
        for i in range(9)  # 6.0 .. 10.0
    ]

    request = {
        "default_max_gap": 0.6,
        "edges": [
            {"parent": "base", "child": "link1", "type": "timed", "keyframes": kf_link1},
            {"parent": "link1", "child": "link2", "type": "timed", "keyframes": kf_link2},
            {
                "parent": "link2",
                "child": "tool",
                "type": "static",
                "transform": {"translation": [0.0, 0.0, 0.15]},
            },
            {
                "parent": "base",
                "child": "sensor",
                "type": "static",
                "transform": {
                    "translation": [0.5, 0.0, 0.3],
                    "rotation": {"axis_angle": {"axis": [0, 0, 1], "angle": np.pi / 2}},
                },
            },
        ],
        "queries": [
            # 1) 正常：t=2.0 三层链累计角 theta1+theta2 = 0.2 rad
            {"source": "tool", "target": "base", "time": 2.0},
            # 2) 正常：插值时刻 t=1.25（link1 在两帧之间）
            {"source": "base", "target": "tool", "time": 1.25},
            # 3) 正常：跨静态传感器分支
            {"source": "sensor", "target": "tool", "time": 0.0},
            # 4) 缺口：t=5.0 落在 link2 的 [4,6] 缺口内
            {"source": "tool", "target": "base", "time": 5.0},
            # 5) 未覆盖：早于最早关键帧（不得拿最新值当历史值）
            {"source": "tool", "target": "base", "time": -0.1},
            # 6) 未覆盖：晚于最晚关键帧（不得外推）
            {"source": "tool", "target": "base", "time": 10.5},
            # 7) 未知坐标系
            {"source": "tool", "target": "gripper", "time": 1.0},
        ],
    }
    return request
