"""CCD 核心验收测试：相切、初始重叠、相同速度、高速穿越。

每个场景的二次方程系数与根均在注释中给出完整手算过程，
断言最早接触时间与手算一致。
"""

import math
import unittest

import numpy as np

from collision_detection import CircleBody, analyze_pair, analyze_request, earliest_collision


def body(center, velocity, radius):
    return CircleBody(center=np.array(center, dtype=float),
                      velocity=np.array(velocity, dtype=float),
                      radius=radius)


class TestHeadOnCollision(unittest.TestCase):
    """对向碰撞基线。

    robot c=(0,0) v=(1,0) r=1；obstacle c=(5,0) v=(-1,0) r=1
    d0=(-5,0), v_rel=(2,0), R=2
    a=4, b=2*(-10)=-20, c=25-4=21
    D=400-336=64 => t=(20±8)/8 => t_enter=1.5, t_exit=3.5
    """

    def test_hand_computed_roots(self):
        r = analyze_pair(body([0, 0], [1, 0], 1), body([5, 0], [-1, 0], 1),
                         t_start=0.0, t_end=4.0)
        self.assertTrue(r.collides)
        self.assertEqual(r.kind, "crossing")
        self.assertAlmostEqual(r.t_enter, 1.5, places=12)
        self.assertAlmostEqual(r.t_exit, 3.5, places=12)
        self.assertAlmostEqual(r.roots[0], 1.5, places=12)
        self.assertAlmostEqual(r.roots[1], 3.5, places=12)

    def test_window_clips_exit(self):
        # 窗口 [0,2] 截断接触区间 [1.5,3.5] => t_exit=2
        r = analyze_pair(body([0, 0], [1, 0], 1), body([5, 0], [-1, 0], 1),
                         t_start=0.0, t_end=2.0)
        self.assertTrue(r.collides)
        self.assertAlmostEqual(r.t_enter, 1.5, places=12)
        self.assertAlmostEqual(r.t_exit, 2.0, places=12)


class TestTangency(unittest.TestCase):
    """相切：判别式为 0 的二重根，按闭区间规则计为碰撞。

    robot c=(0,0) v=(2,0) r=1；obstacle c=(4,3) v=(0,0) r=2
    d0=(-4,-3), v_rel=(2,0), R=3
    a=4, b=2*(-8)=-16, c=25-9=16
    D=256-256=0 => 二重根 t=16/8=2，恰好在 t=2 外切
    """

    def test_tangent_counts_as_collision(self):
        r = analyze_pair(body([0, 0], [2, 0], 1), body([4, 3], [0, 0], 2),
                         t_start=0.0, t_end=3.0)
        self.assertTrue(r.collides)
        self.assertEqual(r.kind, "tangent")
        self.assertAlmostEqual(r.t_enter, 2.0, places=12)
        self.assertAlmostEqual(r.t_exit, 2.0, places=12)

    def test_tangent_outside_window(self):
        # 同一相切时刻 t=2，窗口 [2.5,3] 不含它 => 无碰撞
        r = analyze_pair(body([0, 0], [2, 0], 1), body([4, 3], [0, 0], 2),
                         t_start=2.5, t_end=3.0)
        self.assertFalse(r.collides)


class TestInitialOverlap(unittest.TestCase):
    """初始重叠：t=0 时已重叠，t_enter 取窗口起点。

    robot c=(0,0) v=(1,0) r=2；obstacle c=(3,0) v=(0,0) r=2
    d0=(-3,0), v_rel=(1,0), R=4
    a=1, b=-6, c=9-16=-7
    D=36+28=64 => t=(6±8)/2 => 根 -1, 7；接触区间 [-1,7] 覆盖 t=0
    """

    def test_overlap_enters_at_window_start(self):
        r = analyze_pair(body([0, 0], [1, 0], 2), body([3, 0], [0, 0], 2),
                         t_start=0.0, t_end=5.0)
        self.assertTrue(r.collides)
        self.assertEqual(r.kind, "overlap")
        self.assertAlmostEqual(r.t_enter, 0.0, places=12)
        self.assertAlmostEqual(r.t_exit, 5.0, places=12)  # 窗口截断于 5 < 7

    def test_overlap_full_exit_visible(self):
        # 窗口 [0,10] 覆盖整个接触区间 => t_exit=7
        r = analyze_pair(body([0, 0], [1, 0], 2), body([3, 0], [0, 0], 2),
                         t_start=0.0, t_end=10.0)
        self.assertAlmostEqual(r.t_exit, 7.0, places=12)


class TestSameVelocity(unittest.TestCase):
    """相同速度：a=0，间距恒定，永不碰撞或整窗重叠。"""

    def test_same_velocity_separated_never_collides(self):
        # 间距 5 > R=2，且相对速度为零 => 无碰撞
        r = analyze_pair(body([0, 0], [1, 0.5], 1), body([5, 0], [1, 0.5], 1),
                         t_start=0.0, t_end=4.0)
        self.assertFalse(r.collides)
        self.assertEqual(r.kind, "none")

    def test_same_velocity_overlapping_whole_window(self):
        # 间距 1.5 < R=2，相对速度为零 => 整窗重叠
        r = analyze_pair(body([0, 0], [1, 0.5], 1), body([1.5, 0], [1, 0.5], 1),
                         t_start=0.0, t_end=4.0)
        self.assertTrue(r.collides)
        self.assertEqual(r.kind, "overlap")
        self.assertAlmostEqual(r.t_enter, 0.0, places=12)
        self.assertAlmostEqual(r.t_exit, 4.0, places=12)


class TestHighSpeedTunneling(unittest.TestCase):
    """高速穿越：接触区间极窄，离散采样必漏，解析求根必中。

    robot c=(0,0) v=(1000,0) r=0.1；obstacle c=(500.5,0.19) v=(0,0) r=0.1
    d0=(-500.5,-0.19), v_rel=(1000,0), R=0.2
    a=1e6, b=-1001000, c=250500.2461
    D=1001000^2-4e6*250500.2461=15600 => sqrt(D)≈124.89996
    t=(1001000±124.89996)/2e6 => t_enter≈0.50043755, t_exit≈0.50056245
    接触窗口宽度仅约 1.25e-4 s。
    """

    def setUp(self):
        self.robot = body([0, 0], [1000, 0], 0.1)
        self.obstacle = body([500.5, 0.19], [0, 0], 0.1)

    def test_analytic_detects_tunneling(self):
        r = analyze_pair(self.robot, self.obstacle, t_start=0.0, t_end=1.0)
        self.assertTrue(r.collides)
        self.assertEqual(r.kind, "crossing")
        expected_enter = (1001000 - math.sqrt(15600)) / 2e6
        expected_exit = (1001000 + math.sqrt(15600)) / 2e6
        self.assertAlmostEqual(r.t_enter, expected_enter, places=12)
        self.assertAlmostEqual(r.t_exit, expected_exit, places=12)

    def test_discrete_sampling_would_miss_it(self):
        # 反证“禁止仅靠离散采样”：dt=0.01 采样 101 个点，
        # 没有任何采样时刻两圆接触，但解析解确认发生碰撞。
        radius_sum = self.robot.radius + self.obstacle.radius
        for k in range(101):
            t = k * 0.01
            gap = np.linalg.norm(self.robot.position_at(t)
                                 - self.obstacle.position_at(t))
            self.assertGreater(gap, radius_sum,
                               msg=f"采样点 t={t} 意外落在接触区间内")


class TestNoCollisionAndClosureRules(unittest.TestCase):
    def test_separated_passby_no_collision(self):
        # robot c=(0,0) v=(1,0) r=1；obstacle c=(0,5) v=(0,0) r=1
        # a=1, b=0, c=25-4=21, D=-84<0 => 全程分离
        r = analyze_pair(body([0, 0], [1, 0], 1), body([0, 5], [0, 0], 1),
                         t_start=0.0, t_end=10.0)
        self.assertFalse(r.collides)

    def test_contact_exactly_at_window_end_counts(self):
        # 闭区间规则：接触起点 t=1.5 恰等于窗口终点 => 仍计碰撞
        r = analyze_pair(body([0, 0], [1, 0], 1), body([5, 0], [-1, 0], 1),
                         t_start=0.0, t_end=1.5)
        self.assertTrue(r.collides)
        self.assertAlmostEqual(r.t_enter, 1.5, places=12)

    def test_contact_before_window_start_ignored(self):
        # 接触区间 [1.5,3.5]，窗口 [4,10] => 无碰撞
        r = analyze_pair(body([0, 0], [1, 0], 1), body([5, 0], [-1, 0], 1),
                         t_start=4.0, t_end=10.0)
        self.assertFalse(r.collides)

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            analyze_pair(body([0, 0], [1, 0], 1), body([5, 0], [0, 0], 1),
                         t_start=3.0, t_end=1.0)


class TestMultiObstacle(unittest.TestCase):
    def test_earliest_collision_selected(self):
        # 障碍0：t_enter=1.5；障碍1（静止，c=(10,0)）：t=(40±8)/8 => 4,6
        robot = body([0, 0], [1, 0], 1)
        obstacles = [body([5, 0], [-1, 0], 1), body([10, 0], [-2, 0], 1)]
        results = analyze_request(robot, obstacles, 0.0, 10.0)
        self.assertTrue(all(r.collides for r in results))
        first = earliest_collision(results)
        self.assertIs(first, results[0])
        self.assertAlmostEqual(first.t_enter, 1.5, places=12)

    def test_all_safe_returns_none(self):
        robot = body([0, 0], [0, 0], 1)
        obstacles = [body([10, 10], [0, 0], 1), body([-10, 0], [0, 1], 1)]
        results = analyze_request(robot, obstacles, 0.0, 5.0)
        self.assertIsNone(earliest_collision(results))


if __name__ == "__main__":
    unittest.main()
