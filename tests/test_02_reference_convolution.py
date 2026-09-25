"""2. 整段卷积参考比对(不切换滤波器)。

无切换时,分块流式输出 + 尾部必须与 np.convolve(x, h, 'full')
逐样本一致。
"""

import unittest

import numpy as np

import _bootstrap  # noqa: F401
from fir_stream.engine import StreamingFIREngine
from fir_stream.reference import reference_full_convolution
from fir_stream import filters


class TestReferenceConvolution(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(42)

    def _run(self, x, h, chunk):
        eng = StreamingFIREngine(h)
        ys = []
        for i in range(0, x.shape[0], chunk):
            ys.append(eng.process(x[i:i + chunk]))
        tail = eng.drain()
        ys.append(tail if x.ndim == 2 else tail.ravel())
        return np.concatenate(ys, axis=0)

    def test_random_coeffs_many_chunks(self):
        x = self.rng.standard_normal(2000)
        h = self.rng.standard_normal(33)
        ref = reference_full_convolution(x, h)
        for chunk in [1, 2, 17, 32, 33, 34, 100, 512, 2000]:
            y = self._run(x, h, chunk)
            np.testing.assert_allclose(y.ravel(), ref, atol=1e-12,
                                       err_msg=f"chunk={chunk}")

    def test_designed_filters(self):
        sr = 48000
        x = self.rng.standard_normal(3000)
        for h in [filters.lowpass(2000, 65, sr),
                  filters.highpass(2000, 65, sr),
                  filters.identity(),
                  filters.delay(10)]:
            ref = reference_full_convolution(x, h)
            y = self._run(x, h, 127)
            np.testing.assert_allclose(y.ravel(), ref, atol=1e-12)

    def test_multichannel_matches_independent_convolutions(self):
        x = self.rng.standard_normal((1500, 3))
        hs = [self.rng.standard_normal(n) for n in (5, 17, 31)]
        # 每通道初始即使用不同滤波器,通过按通道 switch 实现
        eng = StreamingFIREngine(hs[0])
        ys = []
        # 先切换通道 1、2 再处理(fade_len=0,通道尚不存在 -> pending,
        # 通道创建时生效);通道 0 使用默认 hs[0]。
        eng.switch(hs[1], channels=[1], fade_len=0)
        eng.switch(hs[2], channels=[2], fade_len=0)
        for i in range(0, 1500, 64):
            ys.append(eng.process(x[i:i + 64]))
        y = np.concatenate(ys + [eng.drain()], axis=0)
        # drain 尾部按全引擎最大阶数对齐;每通道的有效尾部只有 order_ch 个
        max_order = max(h.size - 1 for h in hs)
        for ch in range(3):
            order_ch = hs[ch].size - 1
            ref = reference_full_convolution(x[:, ch], hs[ch])
            ref_padded = np.concatenate(
                [ref, np.zeros(max_order - order_ch)])
            np.testing.assert_allclose(y[:, ch], ref_padded, atol=1e-12,
                                       err_msg=f"channel {ch}")

    def test_output_length(self):
        h = self.rng.standard_normal(33)
        x = self.rng.standard_normal(1000)
        y = self._run(x, h, 256)
        self.assertEqual(y.shape[0], 1000 + 32)


if __name__ == "__main__":
    unittest.main()
