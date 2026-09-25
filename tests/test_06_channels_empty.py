"""6. 通道状态:隔离、通道数变化、空块不改变状态。"""

import unittest

import numpy as np

import _bootstrap  # noqa: F401
from fir_stream.engine import StreamingFIREngine
from fir_stream.reference import LayeredReferenceFIR, reference_full_convolution
from fir_stream import filters


class TestChannelIsolation(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(5)
        self.h0 = self.rng.standard_normal(16)
        self.h1 = self.rng.standard_normal(24)

    def test_switch_one_channel_only(self):
        x = self.rng.standard_normal((2000, 2))
        eng = StreamingFIREngine(self.h0)
        y0 = eng.process(x[:1000])
        eng.switch(self.h1, channels=[1], fade_len=100)
        y1 = eng.process(x[1000:])
        y = np.concatenate([y0, y1, eng.drain()], axis=0)
        # 通道 0 未受影响:整段卷积(drain 尾部按最大阶数对齐,补零比较)
        ref0 = reference_full_convolution(x[:, 0], self.h0)
        ref0 = np.concatenate([ref0, np.zeros(y.shape[0] - ref0.size)])
        np.testing.assert_allclose(y[:, 0], ref0, atol=1e-11)
        # 通道 1 与独立参考一致
        ref = LayeredReferenceFIR(self.h0)
        r0 = ref.process(x[:1000, 1])
        ref.switch(self.h1, fade_len=100)
        r1 = ref.process(x[1000:, 1])
        rt = ref.drain().ravel()
        np.testing.assert_allclose(
            y[:, 1], np.concatenate([r0, r1, rt]), atol=1e-10)

    def test_channels_do_not_share_state(self):
        # 两通道输入完全不同,输出必须等于各自独立引擎的输出
        x = self.rng.standard_normal((1500, 2))
        eng = StreamingFIREngine(self.h0)
        y2 = np.concatenate(
            [eng.process(x[i:i + 64]) for i in range(0, 1500, 64)]
            + [eng.drain()], axis=0)
        for ch in range(2):
            e = StreamingFIREngine(self.h0)
            y1 = np.concatenate(
                [e.process(x[i:i + 64, ch]) for i in range(0, 1500, 64)]
                + [e.drain()[:, 0]], axis=0)
            np.testing.assert_allclose(y2[:, ch], y1, atol=1e-12,
                                       err_msg=f"channel {ch}")


class TestChannelCountChanges(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(6)
        self.h = self.rng.standard_normal(10)

    def test_grow_and_shrink(self):
        x1 = self.rng.standard_normal((500, 1))
        x2 = self.rng.standard_normal((500, 3))
        x3 = self.rng.standard_normal((500, 2))
        eng = StreamingFIREngine(self.h)
        y1 = eng.process(x1)
        y2 = eng.process(x2)
        y3 = eng.process(x3)
        self.assertEqual(y1.shape, (500, 1))  # 二维 (n,1) 输入保持二维
        self.assertEqual(y2.shape, (500, 3))
        self.assertEqual(y3.shape, (500, 2))
        # 通道 0 全程连续:与独立单通道引擎一致
        solo = StreamingFIREngine(self.h)
        s1 = solo.process(x1[:, 0])
        s2 = solo.process(x2[:, 0])
        s3 = solo.process(x3[:, 0])
        np.testing.assert_allclose(y1[:, 0], s1, atol=1e-13)
        np.testing.assert_allclose(y2[:, 0], s2, atol=1e-13)
        np.testing.assert_allclose(y3[:, 0], s3, atol=1e-13)
        # 新通道(1、2)以零历史起步:首块输出 = 从零状态卷积
        np.testing.assert_allclose(
            y2[0, 1], self.h[0] * x2[0, 1], atol=1e-15)
        np.testing.assert_allclose(
            y2[0, 2], self.h[0] * x2[0, 2], atol=1e-15)
        # 通道 2 在第三块消失后,其状态被冻结保留
        self.assertIn(2, eng.channel_ids())

    def test_channel_reappears_with_frozen_state(self):
        eng = StreamingFIREngine(self.h)
        xa = self.rng.standard_normal((100, 2))
        xb = self.rng.standard_normal((100, 1))   # 通道 1 消失
        xc = self.rng.standard_normal((100, 2))   # 通道 1 再现
        eng.process(xa)
        eng.process(xb)
        yc = eng.process(xc)
        # 对照:通道 1 连续处理 xa[:,1] 后接 xc[:,1](中间无输入)
        solo = StreamingFIREngine(self.h)
        solo.process(xa[:, 1])
        yref = solo.process(xc[:, 1])
        np.testing.assert_allclose(yc[:, 1], yref, atol=1e-13)


class TestEmptyBlock(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7)
        self.h = self.rng.standard_normal(12)

    def test_empty_block_no_state_change(self):
        eng = StreamingFIREngine(self.h)
        x = self.rng.standard_normal((300, 2))
        eng.process(x[:100])
        snapshot = {ch: eng.layer_gains(ch) for ch in eng.channel_ids()}
        y = eng.process(np.zeros((0, 2)))
        self.assertEqual(y.shape, (0, 2))
        self.assertEqual({ch: eng.layer_gains(ch) for ch in eng.channel_ids()},
                         snapshot)
        # 空块之后继续处理,与从未插入空块的结果一致
        y_with = eng.process(x[100:])
        eng2 = StreamingFIREngine(self.h)
        eng2.process(x[:100])
        y_without = eng2.process(x[100:])
        np.testing.assert_array_equal(y_with, y_without)

    def test_empty_block_does_not_advance_crossfade(self):
        eng = StreamingFIREngine(self.h)
        eng.process(self.rng.standard_normal(50))
        eng.switch(self.rng.standard_normal(20), fade_len=100)
        gains_before = eng.layer_gains(0)
        for _ in range(5):
            y = eng.process(np.zeros(0))
            self.assertEqual(y.shape, (0,))
        self.assertEqual(eng.layer_gains(0), gains_before)
        self.assertEqual(eng.active_layer_count(0), 2)

    def test_empty_block_creates_no_channels(self):
        eng = StreamingFIREngine(self.h)
        eng.process(np.zeros((0, 4)))
        self.assertEqual(eng.num_channels, 0)

    def test_empty_block_1d_and_2d_shapes(self):
        eng = StreamingFIREngine(self.h)
        self.assertEqual(eng.process(np.zeros(0)).shape, (0,))
        self.assertEqual(eng.process(np.zeros((0, 3))).shape, (0, 3))


if __name__ == "__main__":
    unittest.main()
