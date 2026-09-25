"""5. 切换中再切换(交叉渐变尚未完成时发起新的切换)。"""

import unittest

import numpy as np

import _bootstrap  # noqa: F401
from fir_stream.engine import StreamingFIREngine
from fir_stream.discontinuity import detect_boundary_jumps
from fir_stream.reference import LayeredReferenceFIR
from fir_stream import filters


def run_plan(x, h0, plan):
    """plan: [(block_len, (coeffs, fade_len) 或 None), ...]。

    switch 在处理该块之前应用;block_len 为 0 的条目仅用于表达
    “此处切换”,不会调用 process(空块由专门测试覆盖)。
    """
    eng = StreamingFIREngine(h0)
    ref = LayeredReferenceFIR(h0)
    ye, yr, pos = [], [], 0
    for size, sw in plan:
        if sw is not None:
            eng.switch(sw[0], fade_len=sw[1])
            ref.switch(sw[0], fade_len=sw[1])
        if size:
            blk = x[pos:pos + size]
            ye.append(eng.process(blk))
            yr.append(ref.process(blk))
            pos += size
    ye.append(eng.drain().ravel())
    yr.append(ref.drain().ravel())
    return np.concatenate(ye), np.concatenate(yr)


class TestReswitchDuringFade(unittest.TestCase):
    def setUp(self):
        sr = 48000
        rng = np.random.default_rng(11)
        t = np.arange(8000) / sr
        self.x = 0.5 * np.sin(2 * np.pi * 440 * t) + 0.1 * rng.standard_normal(t.size)
        self.h0 = filters.lowpass(800, 65, sr)
        self.h1 = filters.lowpass(2500, 65, sr)
        self.h2 = filters.highpass(300, 65, sr)
        self.h3 = filters.identity()

    def test_reswitch_mid_fade_matches_reference(self):
        # fade_len=200 的渐变进行到一半时再次切换,随后再切换一次
        plan = [
            (1000, None),
            (0, (self.h1, 200)),
            (100, None),                 # 渐变进行到 100/200
            (0, (self.h2, 150)),         # 再切换
            (700, None),
            (0, (self.h3, 50)),          # 又一次
            (2000, None),
        ]
        y, yref = run_plan(self.x, self.h0, plan)
        np.testing.assert_allclose(y, yref, atol=1e-10)

    def test_rapid_reswitch_gain_sum_invariant(self):
        eng = StreamingFIREngine(self.h0)
        eng.process(self.x[:100])
        # 连续多次切换,渐变全部未完成
        for h, fade in [(self.h1, 500), (self.h2, 400), (self.h3, 300)]:
            eng.switch(h, fade_len=fade)
            eng.process(self.x[100:130])  # 每次只推进 30 个样本
        # 任意时刻增益之和恒为 1
        for _ in range(20):
            eng.process(self.x[130:160])
            self.assertAlmostEqual(sum(eng.layer_gains(0)), 1.0, places=12)
        # 最终收敛到单层
        eng.process(self.x[160:2000])
        self.assertEqual(eng.active_layer_count(0), 1)

    def test_reswitch_no_discontinuity(self):
        plan = [(1000, None),
                (0, (self.h1, 200)),
                (100, None),
                (0, (self.h2, 150)),
                (80, None),
                (0, (self.h3, 60)),
                (3000, None)]
        eng = StreamingFIREngine(self.h0)
        outs, boundaries, pos = [], [], 0
        for size, sw in plan:
            if sw is not None:
                eng.switch(sw[0], fade_len=sw[1])
            if size:
                outs.append(eng.process(self.x[pos:pos + size]))
                pos += size
                boundaries.append(pos)
        outs.append(eng.drain().ravel())
        y = np.concatenate(outs)
        rep = detect_boundary_jumps(y, boundaries[:-1])
        self.assertTrue(rep.ok,
                        f"再切换场景出现边界突跳: {rep.detected_boundaries}")

    def test_reswitch_before_any_input(self):
        # 还没处理过输入就连续切换:pending 语义,不应报错
        eng = StreamingFIREngine(self.h0)
        eng.switch(self.h1, fade_len=100)
        eng.switch(self.h2, fade_len=100)
        y = eng.process(self.x[:500])
        self.assertEqual(y.shape, (500,))
        self.assertEqual(eng.active_layer_count(0), 1)


if __name__ == "__main__":
    unittest.main()
