"""离线回放示例（合成数据，无硬件、无可视化）。

场景：一条 4 坐标系链
    world -> arm -> wrist -> camera
边的运动为合成构造（匀速平移 + 绕 z 轴匀角速度旋转），时间区间 [0, 10] s。

演示内容：
1. 向 TransformTree 写入带时间戳的 SE3 采样；
2. 在非采样时刻查询多边组合变换（平移线性插值 + 旋转 SLERP）；
3. 逆变换与组合的数值误差核对；
4. 故意触发：越界外推、缺失链、成环（均被拒绝）；
5. 异步批量采样（asyncio.to_thread 并发查询）。

运行：
    PYTHONPATH=src python examples/offline_replay.py
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
from scipy.spatial.transform import Rotation

from tf_cache import (
    ExtrapolationNotAllowedError,
    FrameNotFoundError,
    SE3Transform,
    TFCycleError,
    TransformTree,
)


def moving_tf(t: float, velocity: np.ndarray, angular_speed_deg: float) -> SE3Transform:
    """合成运动：匀速平移 + 绕 z 轴匀速旋转。"""
    q = Rotation.from_euler("z", angular_speed_deg * t, degrees=True).as_quat()
    return SE3Transform(velocity * t, q)


def section(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


async def main() -> None:
    tree = TransformTree()

    # 1) 写入合成轨迹：每条边以 2 Hz 采样写入 0..10 s
    section("1. 写入合成轨迹 world->arm->wrist->camera (0..10s, 2Hz)")
    edges = [
        ("world", "arm", np.array([1.0, 0.0, 0.0]), 9.0),
        ("arm", "wrist", np.array([0.0, 1.0, 0.0]), 18.0),
        ("wrist", "camera", np.array([0.0, 0.0, 0.5]), -27.0),
    ]
    sample_times = np.linspace(0.0, 10.0, 21)
    for parent, child, vel, ang in edges:
        for t in sample_times:
            tree.add_transform(parent, child, float(t), moving_tf(t, vel, ang))
        print(f"  写入边 {parent:>6} -> {child:<6}: {len(sample_times)} 个采样")
    print(f"  坐标系: {sorted(tree.frames)}")

    # 2) 在非采样时刻查询多边组合
    section("2. 非采样时刻 t=2.37s 查询 world->camera 组合变换")
    t_query = 2.37
    tf = tree.lookup_transform("camera", "world", t_query)
    print("  T_camera_world =")
    print(np.array2string(tf.to_matrix(), precision=6, suppress_small=False))
    print("  平移:", np.round(tf.translation, 6))

    # 与解析真值核对：角速度叠加 = 9+18-27 = 0（合成构造的总旋转角恒为 0）
    total_angle = np.degrees(Rotation.from_quat(tf.quaternion).magnitude())
    print(f"  合成总旋转角: {total_angle:.6e} deg (解析真值: 0)")
    assert total_angle < 1e-8

    # 3) 组合后再逆变换的数值误差
    section("3. 组合后再逆变换的数值误差（多点扫描）")
    max_trans_err = 0.0
    max_rot_err = 0.0
    for t in np.linspace(0.0, 10.0, 1001):
        fwd = tree.lookup_transform("camera", "world", float(t))
        inv = tree.lookup_transform("world", "camera", float(t))
        residual = fwd.multiply(inv).to_matrix() - np.eye(4)
        max_trans_err = max(max_trans_err, np.linalg.norm(residual[:3, 3]))
        max_rot_err = max(max_rot_err, np.linalg.norm(residual[:3, :3]))
    print(f"  1001 个时刻扫描，最大平移残差: {max_trans_err:.3e}")
    print(f"  1001 个时刻扫描，最大旋转残差: {max_rot_err:.3e}")

    # 点往返测试
    rng = np.random.default_rng(0)
    pts = rng.normal(size=(5, 3))
    fwd = tree.lookup_transform("camera", "world", 5.5)
    inv = tree.lookup_transform("world", "camera", 5.5)
    back = inv.apply(fwd.apply(pts))
    print(f"  点往返最大误差: {np.abs(back - pts).max():.3e}")

    # 4) 被拒绝的操作
    section("4. 拒绝越界外推 / 缺失链 / 成环")
    for t in (-0.1, 10.5):
        try:
            tree.lookup_transform("world", "camera", t)
        except ExtrapolationNotAllowedError as e:
            print(f"  [拒绝外推] t={t}: {e}")

    try:
        tree.lookup_transform("world", "lidar", 1.0)
    except FrameNotFoundError as e:
        print(f"  [缺失链] {e}")

    try:
        tree.add_transform("camera", "world", 1.0, SE3Transform.identity())
    except TFCycleError as e:
        print(f"  [拒绝成环] {e}")

    # 5) 异步批量采样
    section("5. 异步并发批量采样（16 个协程 x 501 时刻）")
    times = np.linspace(0.0, 10.0, 501).tolist()

    async def sample_batch():
        return await asyncio.to_thread(tree.lookup_transforms, "world", "camera", times)

    t0 = time.perf_counter()
    batches = await asyncio.gather(*[sample_batch() for _ in range(16)])
    elapsed = time.perf_counter() - t0
    ref = batches[0]
    consistent = all(
        np.allclose(a.to_matrix(), b.to_matrix(), atol=1e-11)
        for batch in batches[1:]
        for a, b in zip(batch, ref)
    )
    print(f"  并发 16 批 x {len(times)} 时刻，耗时 {elapsed*1000:.1f} ms")
    print(f"  各批结果完全一致: {consistent}")
    print(f"  抽查 t=5.000s 平移: {np.round(ref[250].translation, 6)}")

    section("回放完成")


if __name__ == "__main__":
    asyncio.run(main())
