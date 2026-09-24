"""核心库测试：SE3 / TimeCache / TransformTree。

验收点覆盖：
- 90 度旋转（及 45 度插值）
- 四元数反号等价（double cover）
- 逆变换 / 组合后再逆的数值误差
- 多边组合、缺失链、成环拒绝、越界拒绝、异步批量采样
"""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from tf_cache.errors import (
    DuplicateTimestampError,
    ExtrapolationNotAllowedError,
    FrameNotFoundError,
    InvalidTransformError,
    TFCycleError,
)
from tf_cache.se3 import SE3Transform, slerp
from tf_cache.tree import TimeCache, TransformTree

IDENTITY_Q = np.array([0.0, 0.0, 0.0, 1.0])
RTOL = 1e-12


def qz(angle_deg: float) -> np.ndarray:
    """绕 z 轴旋转 angle_deg 度的四元数 [x,y,z,w]。"""
    return Rotation.from_euler("z", angle_deg, degrees=True).as_quat()


def make_tf(translation=(0, 0, 0), quaternion=IDENTITY_Q) -> SE3Transform:
    return SE3Transform(translation, quaternion)


# --------------------------------------------------------------------------
# 90 度旋转
# --------------------------------------------------------------------------
class Test90DegreeRotation:
    def test_rotation_of_point_about_z(self):
        tf = make_tf(quaternion=qz(90))
        p = tf.apply(np.array([1.0, 0.0, 0.0]))
        np.testing.assert_allclose(p, [0.0, 1.0, 0.0], atol=1e-12)

    def test_matrix_matches_scipy(self):
        tf = make_tf(translation=[1, 2, 3], quaternion=qz(90))
        expected = np.eye(4)
        expected[:3, :3] = Rotation.from_euler("z", 90, degrees=True).as_matrix()
        expected[:3, 3] = [1, 2, 3]
        np.testing.assert_allclose(tf.to_matrix(), expected, atol=1e-12)

    def test_slerp_midpoint_is_45_degrees(self):
        q_mid = slerp(IDENTITY_Q, qz(90), 0.5)
        angle = Rotation.from_quat(q_mid).magnitude()
        assert np.degrees(angle) == pytest.approx(45.0, abs=1e-10)
        p = SE3Transform(np.zeros(3), q_mid).apply([1, 0, 0])
        np.testing.assert_allclose(p, [np.cos(np.pi / 4), np.sin(np.pi / 4), 0], atol=1e-12)

    def test_slerp_endpoints(self):
        np.testing.assert_allclose(slerp(IDENTITY_Q, qz(90), 0.0), IDENTITY_Q, atol=1e-12)
        np.testing.assert_allclose(slerp(IDENTITY_Q, qz(90), 1.0), qz(90), atol=1e-12)

    def test_timecache_rotation_interpolation(self):
        cache = TimeCache("a", "b")
        cache.add_sample(0.0, make_tf(quaternion=IDENTITY_Q))
        cache.add_sample(1.0, make_tf(quaternion=qz(90)))
        tf = cache.lookup(0.25)
        angle = Rotation.from_quat(tf.quaternion).magnitude()
        assert np.degrees(angle) == pytest.approx(22.5, abs=1e-10)


# --------------------------------------------------------------------------
# 四元数反号等价（q == -q）
# --------------------------------------------------------------------------
class TestQuaternionDoubleCover:
    def test_negated_quaternion_same_rotation(self):
        q = qz(90)
        tf_pos = make_tf(translation=[0.1, -0.2, 0.3], quaternion=q)
        tf_neg = make_tf(translation=[0.1, -0.2, 0.3], quaternion=-q)
        np.testing.assert_allclose(
            tf_pos.rotation_matrix(), tf_neg.rotation_matrix(), atol=1e-12
        )
        p = np.array([0.7, -0.4, 0.2])
        np.testing.assert_allclose(tf_pos.apply(p), tf_neg.apply(p), atol=1e-12)
        assert tf_pos == tf_neg

    def test_slerp_takes_short_arc_across_sign_flip(self):
        # 末帧故意用 -q 写入；SLERP 必须识别反号等价并走短弧
        cache = TimeCache("a", "b")
        cache.add_sample(0.0, make_tf(quaternion=IDENTITY_Q))
        cache.add_sample(1.0, make_tf(quaternion=-qz(90)))
        mid = cache.lookup(0.5)
        angle = Rotation.from_quat(mid.quaternion).magnitude()
        # 若错误地走长弧，角度会是 180-45=135 度
        assert np.degrees(angle) == pytest.approx(45.0, abs=1e-9)

    def test_slerp_path_continuous_through_antipodal_samples(self):
        # 连续多帧，相邻帧四元数符号任意，插值角度必须单调
        qs = [qz(a) if a % 2 == 0 else -qz(a) for a in range(0, 181, 30)]
        cache = TimeCache("a", "b")
        for i, q in enumerate(qs):
            cache.add_sample(float(i), make_tf(quaternion=q))
        angles = [
            np.degrees(Rotation.from_quat(cache.lookup(i + 0.5).quaternion).magnitude())
            for i in range(len(qs) - 1)
        ]
        expected = [a + 15 for a in range(0, 180, 30)]
        np.testing.assert_allclose(angles, expected, atol=1e-9)


# --------------------------------------------------------------------------
# 平移线性插值
# --------------------------------------------------------------------------
class TestTranslationInterpolation:
    def test_linear_interpolation(self):
        cache = TimeCache("a", "b")
        cache.add_sample(0.0, make_tf(translation=[0, 0, 0]))
        cache.add_sample(2.0, make_tf(translation=[2, 4, 6]))
        tf = cache.lookup(1.0)
        np.testing.assert_allclose(tf.translation, [1, 2, 3], atol=1e-12)

    def test_endpoint_snap(self):
        cache = TimeCache("a", "b")
        cache.add_sample(1.0, make_tf(translation=[5, 5, 5]))
        cache.add_sample(2.0, make_tf(translation=[6, 6, 6]))
        tf = cache.lookup(1.0 + 1e-12)
        np.testing.assert_allclose(tf.translation, [5, 5, 5], atol=1e-11)


# --------------------------------------------------------------------------
# 逆变换与组合数值误差
# --------------------------------------------------------------------------
class TestInverseAndComposition:
    def test_inverse_product_is_identity(self):
        rng = np.random.default_rng(42)
        for _ in range(20):
            q = Rotation.random(random_state=rng).as_quat()
            t = rng.normal(size=3)
            tf = make_tf(translation=t, quaternion=q)
            prod = tf.multiply(tf.inverse())
            np.testing.assert_allclose(prod.to_matrix(), np.eye(4), atol=1e-12)

    def test_compose_then_inverse_roundtrip(self):
        """多边组合 f0->f1->f2->f3，再与反向链相乘，核对数值误差。"""
        rng = np.random.default_rng(7)
        tree = TransformTree()
        rots = [Rotation.random(random_state=rng).as_quat() for _ in range(3)]
        trans = [rng.normal(size=3) for _ in range(3)]
        for i, (t, q) in enumerate(zip(trans, rots)):
            tree.add_transform(f"f{i}", f"f{i + 1}", 0.0, make_tf(t, q))

        # source=f3 -> target=f0 返回 T_f0_f3 = E0·E1·E2
        f0_f3 = tree.lookup_transform("f3", "f0", 0.0)
        # source=f0 -> target=f3 返回其逆 T_f3_f0
        f3_f0 = tree.lookup_transform("f0", "f3", 0.0)

        # 参考值：E0·E1·E2（E0 为最外层），从最内层 E2 开始外乘
        ref = make_tf()
        for t, q in zip(reversed(trans), reversed(rots)):
            ref = make_tf(t, q).multiply(ref)
        np.testing.assert_allclose(f0_f3.to_matrix(), ref.to_matrix(), atol=RTOL)

        # 组合与逆组合相乘应为单位阵
        residual = f0_f3.multiply(f3_f0)
        err_trans = np.linalg.norm(residual.translation)
        err_rot = np.linalg.norm(residual.rotation_matrix() - np.eye(3))
        assert err_trans < 1e-11, f"组合后逆变换平移残差 {err_trans}"
        assert err_rot < 1e-11, f"组合后逆变换旋转残差 {err_rot}"

    def test_round_trip_points_across_chain(self):
        """点经正向链变换再经反向链变换应回到原点。"""
        tree = TransformTree()
        tree.add_transform("world", "a", 1.0, make_tf([1, 0, 0], qz(30)))
        tree.add_transform("a", "b", 1.0, make_tf([0, 2, 0], qz(60)))
        tree.add_transform("b", "c", 1.0, make_tf([0, 0, 5], qz(-45)))

        forward = tree.lookup_transform("c", "world", 1.0)   # world->c
        backward = tree.lookup_transform("world", "c", 1.0)  # c->world
        pts = np.array([[0.0, 0, 0], [1, 1, 1], [-2, 3, 0.5]])
        mapped = backward.apply(forward.apply(pts))
        np.testing.assert_allclose(mapped, pts, atol=1e-12)

    def test_single_frame_lookup_identity(self):
        tree = TransformTree()
        tree.add_transform("w", "a", 0.0, make_tf([1, 2, 3], qz(90)))
        ident = tree.lookup_transform("w", "w", 0.0)
        np.testing.assert_allclose(ident.to_matrix(), np.eye(4), atol=1e-15)


# --------------------------------------------------------------------------
# 树结构：成环 / 缺失链 / 反向写入
# --------------------------------------------------------------------------
class TestTreeStructure:
    def _chain_tree(self) -> TransformTree:
        tree = TransformTree()
        # 两条边都在 0、1 秒采样，保证任意时刻查询整条链都有覆盖
        for t in (0.0, 1.0):
            tree.add_transform("w", "a", t, make_tf([1, 0, 0], qz(90)))
            tree.add_transform("a", "b", t, make_tf([0, 1, 0], qz(45)))
        return tree

    def test_cycle_rejected(self):
        tree = self._chain_tree()
        with pytest.raises(TFCycleError):
            tree.add_transform("b", "w", 0.0, make_tf())

    def test_cycle_rejected_longer_chain(self):
        tree = TransformTree()
        names = ["f0", "f1", "f2", "f3", "f4"]
        for a, b in zip(names, names[1:]):
            tree.add_transform(a, b, 0.0, make_tf())
        with pytest.raises(TFCycleError):
            tree.add_transform("f4", "f0", 0.0, make_tf())

    def test_self_loop_rejected(self):
        tree = TransformTree()
        with pytest.raises(TFCycleError):
            tree.add_transform("x", "x", 0.0, make_tf())

    def test_missing_frame(self):
        tree = self._chain_tree()
        with pytest.raises(FrameNotFoundError):
            tree.lookup_transform("w", "ghost", 0.0)
        with pytest.raises(FrameNotFoundError):
            tree.find_path("w", "ghost")

    def test_disconnected_chain(self):
        tree = self._chain_tree()
        tree.add_transform("x", "y", 0.0, make_tf())  # 独立连通分量
        with pytest.raises(FrameNotFoundError):
            tree.lookup_transform("w", "y", 0.0)

    def test_reverse_insertion_takes_inverse(self):
        tree = TransformTree()
        tf = make_tf([1, 2, 3], qz(60))
        tree.add_transform("w", "a", 0.0, tf)
        # 按反方向再写一个采样，应等价于写入逆变换
        tree.add_transform("a", "w", 1.0, SE3Transform.identity())
        out = tree.lookup_transform("a", "w", 1.0)
        np.testing.assert_allclose(out.to_matrix(), np.eye(4), atol=1e-12)

    def test_path_is_undirected(self):
        tree = self._chain_tree()
        assert tree.find_path("w", "b") == ["w", "a", "b"]
        assert tree.find_path("b", "w") == ["b", "a", "w"]

    def test_reverse_lookup_equals_inverse(self):
        tree = self._chain_tree()
        fwd = tree.lookup_transform("b", "w", 1.0)
        rev = tree.lookup_transform("w", "b", 1.0)
        np.testing.assert_allclose(
            fwd.multiply(rev).to_matrix(), np.eye(4), atol=1e-12
        )

    def test_lookup_direction_convention(self):
        """显式核对 source/target 方向约定：

        写入 T_parent_child 后，lookup(child->parent) 必须返回写入值，
        lookup(parent->child) 必须返回其逆；多边链用点坐标解析验证。
        """
        tree = TransformTree()
        tf = make_tf([1.0, 2.0, 3.0], qz(90))
        tree.add_transform("w", "a", 0.0, tf)
        # child -> parent：写入值本身
        np.testing.assert_allclose(
            tree.lookup_transform("a", "w", 0.0).to_matrix(),
            tf.to_matrix(),
            atol=1e-12,
        )
        # parent -> child：逆变换
        np.testing.assert_allclose(
            tree.lookup_transform("w", "a", 0.0).to_matrix(),
            tf.inverse().to_matrix(),
            atol=1e-12,
        )

        # 多边链解析验证：a 原点在 w 中 (1,2,3)，b 原点在 a 中 (0,2,0)
        tree.add_transform("a", "b", 0.0, make_tf([0, 2, 0], IDENTITY_Q))
        # b 原点在 w：t = (1,2,3) + Rz(90)·(0,2,0) = (1,2,3)+(-2,0,0) = (-1,2,3)
        t_b_in_w = tree.lookup_transform("b", "w", 0.0).translation
        np.testing.assert_allclose(t_b_in_w, [-1, 2, 3], atol=1e-12)
        # 反方向 T_b_w：t = t_ab^-1 + R^T 推导结果 (-2,-1,-3)
        t_w_in_b = tree.lookup_transform("w", "b", 0.0).translation
        np.testing.assert_allclose(t_w_in_b, [-2, -1, -3], atol=1e-12)


# --------------------------------------------------------------------------
# 时间处理：外推拒绝 / 重复时间戳 / 批量采样 / 异步
# --------------------------------------------------------------------------
class TestTimeHandling:
    def _tree(self) -> TransformTree:
        # 单位旋转链，使组合平移随时间严格线性：x 0->2（w->a）、y 0->4（a->b）
        tree = TransformTree()
        tree.add_transform("w", "a", 0.0, make_tf([0, 0, 0], IDENTITY_Q))
        tree.add_transform("w", "a", 1.0, make_tf([2, 0, 0], IDENTITY_Q))
        tree.add_transform("a", "b", 0.0, make_tf([0, 0, 0], IDENTITY_Q))
        tree.add_transform("a", "b", 1.0, make_tf([0, 4, 0], IDENTITY_Q))
        return tree

    def test_extrapolation_before_range(self):
        tree = self._tree()
        with pytest.raises(ExtrapolationNotAllowedError):
            tree.lookup_transform("b", "w", -0.001)

    def test_extrapolation_after_range(self):
        tree = self._tree()
        with pytest.raises(ExtrapolationNotAllowedError):
            tree.lookup_transform("b", "w", 1.001)

    def test_lookup_at_exact_boundaries(self):
        tree = self._tree()
        tf0 = tree.lookup_transform("b", "w", 0.0)
        tf1 = tree.lookup_transform("b", "w", 1.0)
        np.testing.assert_allclose(tf0.translation, [0, 0, 0], atol=1e-12)
        np.testing.assert_allclose(tf1.translation, [2, 4, 0], atol=1e-12)

    def test_duplicate_timestamp_rejected(self):
        cache = TimeCache("a", "b")
        cache.add_sample(0.0, make_tf())
        with pytest.raises(DuplicateTimestampError):
            cache.add_sample(0.0, make_tf())

    def test_out_of_order_rejected(self):
        cache = TimeCache("a", "b")
        cache.add_sample(1.0, make_tf())
        with pytest.raises(ValueError):
            cache.add_sample(0.5, make_tf())

    def test_batch_lookup_same_path(self):
        tree = self._tree()
        times = [0.0, 0.25, 0.5, 0.75, 1.0]
        # b->w：T_w_b，单位旋转下平移即 b 原点在 w 中位置 (x, y)
        tfs = tree.lookup_transforms("b", "w", times)
        xs = [tf.translation[0] for tf in tfs]
        ys = [tf.translation[1] for tf in tfs]
        np.testing.assert_allclose(xs, [0, 0.5, 1.0, 1.5, 2.0], atol=1e-12)
        np.testing.assert_allclose(ys, [0, 1.0, 2.0, 3.0, 4.0], atol=1e-12)

    def test_async_concurrent_sampling(self):
        """多线程并发批量采样：线程安全且结果与串行一致。"""
        tree = self._tree()
        times = [i / 64 for i in range(65)]
        expected = tree.lookup_transforms("b", "w", times)

        results: list[list[SE3Transform] | None] = [None] * 8

        def worker(idx: int) -> None:
            results[idx] = tree.lookup_transforms("b", "w", times)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        for got in results:
            assert got is not None
            for a, b in zip(got, expected):
                np.testing.assert_allclose(a.to_matrix(), b.to_matrix(), atol=1e-11)

    def test_async_batch_under_asyncio(self):
        """事件循环中把批量查询放到线程执行器，验证异步采样工作流。"""
        tree = self._tree()
        times = [0.1 * i for i in range(11)]

        async def scenario() -> list[list[SE3Transform]]:
            coros = [
                asyncio.to_thread(tree.lookup_transforms, "b", "w", times)
                for _ in range(4)
            ]
            return await asyncio.gather(*coros)

        batches = asyncio.run(scenario())
        ref = tree.lookup_transforms("b", "w", times)
        for batch in batches:
            for a, b in zip(batch, ref):
                np.testing.assert_allclose(a.to_matrix(), b.to_matrix(), atol=1e-11)


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------
class TestValidation:
    def test_nonunit_quaternion_rejected(self):
        with pytest.raises(InvalidTransformError):
            make_tf(quaternion=[2.0, 0.0, 0.0, 0.0])  # 模长 2
        # 模长为 1 的四元数是合法的（[1,0,0,0] = 绕 x 轴 180 度）
        make_tf(quaternion=[1.0, 0.0, 0.0, 0.0])

    def test_zero_quaternion_rejected(self):
        with pytest.raises(InvalidTransformError):
            make_tf(quaternion=[0, 0, 0, 0])

    def test_nan_rejected(self):
        with pytest.raises(InvalidTransformError):
            make_tf(translation=[np.nan, 0, 0], quaternion=IDENTITY_Q)
        with pytest.raises(InvalidTransformError):
            make_tf(translation=[0, 0, 0], quaternion=[0, 0, np.nan, 1])

    def test_bad_shape_rejected(self):
        with pytest.raises(InvalidTransformError):
            SE3Transform([1, 2], IDENTITY_Q)
        with pytest.raises(InvalidTransformError):
            SE3Transform([0, 0, 0], [1, 2, 3])

    def test_interpolate_outside_segment_rejected(self):
        a = make_tf()
        b = make_tf([1, 0, 0])
        with pytest.raises(InvalidTransformError):
            SE3Transform.interpolate(a, b, 1.1)

    def test_quaternion_near_unit_is_normalized(self):
        q = np.array([0.0, 0.0, 0.0, 1.0 + 1e-6])
        tf = make_tf(quaternion=q)
        assert np.linalg.norm(tf.quaternion) == pytest.approx(1.0, abs=1e-15)
