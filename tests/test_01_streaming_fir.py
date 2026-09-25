"""1. StreamingFIR 单通道流式滤波器单元测试。"""

import unittest

import numpy as np

import _bootstrap  # noqa: F401
from fir_stream.core import StreamingFIR
from fir_stream.reference import reference_full_convolution


class TestStreamingFIR(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(0)

    def test_matches_full_convolution_no_tail(self):
        # mode="same-length":对随机输入逐块处理,截断到输入长度的部分
        # 必须与整段卷积逐样本一致(无尾部时)。
        h = self.rng.standard_normal(15)
        x = self.rng.standard_normal(1000)
        full = reference_full_convolution(x, h)

        for block in [1, 7, 15, 16, 100, 1000]:
            f = StreamingFIR(h)
            ys = []
            for i in range(0, x.size, block):
                ys.append(f.process(x[i:i + block]))
            y = np.concatenate(ys)
            np.testing.assert_allclose(
                y, full[:x.size], atol=1e-12,
                err_msg=f"block={block}")

    def test_drain_completes_full_convolution(self):
        h = np.array([1.0, 0.5, -0.25])
        x = np.array([1.0, 2.0, 3.0, 4.0])
        f = StreamingFIR(h)
        y = f.process(x)
        tail = f.process(np.zeros(h.size - 1))
        np.testing.assert_allclose(
            np.concatenate([y, tail]), reference_full_convolution(x, h),
            atol=1e-14)

    def test_state_isolated_between_instances(self):
        h = self.rng.standard_normal(8)
        x = self.rng.standard_normal(100)
        f1 = StreamingFIR(h)
        f2 = StreamingFIR(h)
        f1.process(x[:50])
        # f2 从未处理任何东西:状态必须保持零
        np.testing.assert_array_equal(f2.state, np.zeros(7))
        # f1 的状态 = 最近 7 个输入,按时间顺序(最旧在前)
        np.testing.assert_allclose(f1.state, x[43:50])

    def test_prime_with_history(self):
        h = self.rng.standard_normal(10)
        history = self.rng.standard_normal(9)
        x = self.rng.standard_normal(50)
        f1 = StreamingFIR(h)
        f1.process(history)
        y1 = f1.process(x)

        f2 = StreamingFIR(h)
        f2.prime(history)
        y2 = f2.process(x)
        np.testing.assert_array_equal(f1.state, f2.state)
        np.testing.assert_allclose(y1, y2, atol=1e-14)

    def test_prime_longer_history_uses_tail(self):
        h = np.array([1.0, 2.0, 3.0])
        f = StreamingFIR(h)
        f.prime(np.arange(20, dtype=float))
        np.testing.assert_array_equal(f.state, [18.0, 19.0])

    def test_empty_block_is_noop(self):
        h = self.rng.standard_normal(12)
        f = StreamingFIR(h)
        y = f.process(np.zeros(0))
        self.assertEqual(y.shape, (0,))
        np.testing.assert_array_equal(f.state, np.zeros(11))
        f.process(self.rng.standard_normal(37))
        state_before = f.state
        y2 = f.process(np.zeros(0))
        self.assertEqual(y2.shape, (0,))
        np.testing.assert_array_equal(f.state, state_before)
        # 空块之后继续处理,结果与没有空块完全一致
        cont = f.process(np.arange(100, dtype=float))
        f3 = StreamingFIR(h)
        f3.process(np.zeros(0))
        self.assertEqual(f3.process(self.rng.standard_normal(37)).shape, (37,))

    def test_one_tap_identity(self):
        f = StreamingFIR([2.0])
        x = self.rng.standard_normal(50)
        np.testing.assert_allclose(f.process(x), 2.0 * x)
        self.assertEqual(f.state.shape, (0,))
        f.prime(self.rng.standard_normal(5))
        self.assertEqual(f.state.shape, (0,))

    def test_invalid_coeffs(self):
        with self.assertRaises(ValueError):
            StreamingFIR([])
        with self.assertRaises(ValueError):
            StreamingFIR([1.0, 0.0], state=np.zeros(2))


if __name__ == "__main__":
    unittest.main()
