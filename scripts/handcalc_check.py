#!/usr/bin/env python3
"""独立手算复核脚本（验收留痕）。

不使用 pytest 断言：所有"期望值"都在本文件里用纸面公式直接算出，
再与库的输出逐项打印对比，输出 PASS/FAIL。任何 FAIL 退出码为 1。

手算模型（旋转全部绕 z 轴；记 Rz(a)、c=cos a、s=sin a）：

链：base -> arm -> wrist
  T_base_arm  @t=2 : Rz(90°),  t=(1,0,0)
  T_arm_wrist @t=2 : Rz(90°),  t=(0,1,0)
  T_base_wrist = T_base_arm * T_arm_wrist
    R = Rz(180°) = [[-1,0,0],[0,-1,0],[0,0,1]]
    t = Rz(90°)·(0,1,0) + (1,0,0) = (-1,0,0)+(1,0,0) = (0,0,0)
    点 p_wrist=(1,0,0) -> p_base=(-1,0,0)
  逆 T_wrist_base：R=Rz(180°)（自逆），t=(0,0,0)
  单段逆 T_wrist_arm：R=Rz(-90°)，t=-Rz(-90°)·(0,1,0)=(-1,0,0)

插值 @t=1（两段都从"单位变换"线性变到 t=2 的姿态）：
  每段 R=Rz(45°)，平移取端点一半：
    t_base_arm=(0.5,0,0)，t_arm_wrist=(0,0.5,0)
  复合 R=Rz(90°)
    t = Rz(45°)·(0,0.5,0)+(0.5,0,0)
      = (0.5 - 0.5·sin45°, 0.5·cos45°, 0)
      = (0.1464466094, 0.3535533906, 0)
"""

from __future__ import annotations

import sys

import numpy as np

from transform_tree.timed_sequence import Keyframe, TimedTransformSequence
from transform_tree.transform import Transform
from transform_tree.tree import TransformTree

FAIL_COUNT = 0


def check(name: str, actual: np.ndarray, expected: np.ndarray, tol: float = 1e-10) -> None:
    global FAIL_COUNT
    ok = np.allclose(actual, expected, atol=tol)
    status = "PASS" if ok else "FAIL"
    if not ok:
        FAIL_COUNT += 1
    print(f"[{status}] {name}")
    print(f"        expected = {np.asarray(expected).ravel().round(10).tolist()}")
    print(f"        actual   = {np.asarray(actual).ravel().round(10).tolist()}")


def rz(angle_rad: float) -> np.ndarray:
    c, s = np.cos(angle_rad), np.sin(angle_rad)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def build_tree() -> TransformTree:
    rz90 = rz(np.pi / 2)
    tree = TransformTree()
    tree.add_edge(
        "base",
        "arm",
        TimedTransformSequence(
            [
                Keyframe(0.0, Transform(np.eye(3), np.zeros(3))),
                Keyframe(2.0, Transform(rz90, np.array([1.0, 0.0, 0.0]))),
            ]
        ),
    )
    tree.add_edge(
        "arm",
        "wrist",
        TimedTransformSequence(
            [
                Keyframe(0.0, Transform(np.eye(3), np.zeros(3))),
                Keyframe(2.0, Transform(rz90, np.array([0.0, 1.0, 0.0]))),
            ]
        ),
    )
    return tree


def main() -> int:
    global FAIL_COUNT
    tree = build_tree()

    print("== 1) 三层链 T_base_wrist @t=2 ==")
    t_fwd = tree.lookup_transform("base", "wrist", 2.0)
    check("rotation = Rz(180°)", t_fwd.rotation, rz(np.pi))
    check("translation = (0,0,0)", t_fwd.translation, [0, 0, 0])
    check("p_wrist(1,0,0) -> p_base(-1,0,0)", t_fwd.transform_point([1, 0, 0]), [-1, 0, 0])

    print("== 2) 逆变换 T_wrist_base @t=2 ==")
    t_inv = tree.lookup_transform("wrist", "base", 2.0)
    check("rotation = Rz(180°)", t_inv.rotation, rz(np.pi))
    check("translation = (0,0,0)", t_inv.translation, [0, 0, 0])
    check("inv * fwd = I", (t_inv @ t_fwd).to_matrix(), np.eye(4))

    print("== 3) 单段逆 T_wrist_arm @t=2 ==")
    t_seg = tree.lookup_transform("wrist", "arm", 2.0)
    check("rotation = Rz(-90°)", t_seg.rotation, rz(-np.pi / 2))
    check("translation = (-1,0,0)", t_seg.translation, [-1, 0, 0])

    print("== 4) 旋转插值 T_base_wrist @t=1 ==")
    t_mid = tree.lookup_transform("base", "wrist", 1.0)
    check("rotation = Rz(90°)", t_mid.rotation, rz(np.pi / 2))
    expected_trans = [0.5 - 0.5 * np.sin(np.pi / 4), 0.5 * np.cos(np.pi / 4), 0.0]
    check("translation = (0.14645, 0.35355, 0)", t_mid.translation, expected_trans)

    print("== 5) 时间覆盖：越界必须报错 ==")
    from transform_tree.errors import TimeGapError, TimeNotCoveredError

    for bad_t, label in [(-0.5, "早于首帧"), (2.5, "晚于末帧")]:
        try:
            tree.lookup_transform("base", "wrist", bad_t)
        except TimeNotCoveredError:
            print(f"[PASS] {label} t={bad_t} -> TimeNotCoveredError")
        else:
            FAIL_COUNT += 1
            print(f"[FAIL] {label} t={bad_t} 未报错（错误地外推/沿用末帧！）")

    print("== 6) 时刻缺口：间隔超阈值必须报错 ==")
    gapped = TransformTree()
    gapped.add_edge(
        "base",
        "arm",
        TimedTransformSequence(
            [
                Keyframe(0.0, Transform(np.eye(3), np.zeros(3))),
                Keyframe(1.0, Transform(rz(np.pi / 2), np.zeros(3))),
                Keyframe(4.0, Transform(rz(np.pi / 2), np.zeros(3))),
            ],
            max_gap=0.6,
        ),
    )
    try:
        gapped.lookup_transform("base", "arm", 2.0)
    except TimeGapError:
        print("[PASS] t=2.0 落在 3s 缺口（阈值 0.6s）-> TimeGapError")
    else:
        FAIL_COUNT += 1
        print("[FAIL] 缺口时刻未报错")

    print()
    if FAIL_COUNT:
        print(f"手算复核存在 {FAIL_COUNT} 项 FAIL")
        return 1
    print("手算复核全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
