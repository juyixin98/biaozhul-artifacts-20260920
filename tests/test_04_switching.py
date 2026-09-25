"""4. 运行中滤波器切换 + 交叉渐变无突跳。"""

import unittest

import numpy as np

import _bootstrap  # noqa: F401
from fir_stream.engine import StreamingFIREngine
from fir_stream.discontinuity import detect_boundary_jumps
from fir_stream.reference import LayeredReferenceFIR, reference_full_convolution
from fir_stream import filters


def process_with_switch_at(x, h0, h1, at, fade_len, engine_cls):
    eng = engine_cls(h0)
    y0 = eng.process(x[:at])
    eng.switch(h1, fade_len=fade_len)
    y1 = eng.process(x[at:])
    tail = eng.drain()
    tail = tail if x.ndim == 2 else tail.ravel()
    return np.concatenate([y0, y1, tail])


class TestSwitching(unittest.TestCase):
    def setUp(self):
        sr = 48000
        self.sr = sr
        t = np.arange(12000) / sr
        self.x = 0.6 * (np.sin(2 * np.pi * 300 * t)
                        + 0.5 * np.sin(2 * np.pi * 1800 * t))
        self.h0 = filters.lowpass(1000, 65, sr)
        self.h1 = filters.lowpass(3000, 65, sr)
        self.at = 5000

    def test_crossfade_matches_independent_reference(self):
        for fade in (1, 32, 128, 257, 1000):
            y = process_with_switch_at(
                self.x, self.h0, self.h1, self.at, fade, StreamingFIREngine)
            yref = process_with_switch_at(
                self.x, self.h0, self.h1, self.at, fade, LayeredReferenceFIR)
            np.testing.assert_allclose(y, yref, atol=1e-10,
                                       err_msg=f"fade={fade}")

    def test_gain_sum_is_one_throughout_fade(self):
        eng = StreamingFIREngine(self.h0)
        eng.process(self.x[:self.at])
        eng.switch(self.h1, fade_len=100)
        # 淡入期间任意点,所有层增益之和恒为 1
        for k in (1, 37, 100):
            eng.process(np.zeros(k))
            gains = eng.layer_gains(0)
            self.assertAlmostEqual(sum(gains), 1.0, places=12)
        self.assertEqual(eng.active_layer_count(0), 1)

    def test_no_discontinuity_at_switch_point(self):
        fade = 256
        y = process_with_switch_at(
            self.x, self.h0, self.h1, self.at, fade, StreamingFIREngine)
        rep = detect_boundary_jumps(
            y, [self.at], abs_floor=1e-9)
        self.assertTrue(rep.ok, f"切换点出现突跳: {rep.detected_values}")
        # 切换点处的一阶差分应与信号正常变化水平相当
        self.assertLess(rep.boundary_max_abs_diff,
                        rep.global_max_abs_diff * 1.5)

    def test_hard_switch_replaces_layers(self):
        eng = StreamingFIREngine(self.h0)
        eng.process(self.x[:100])
        eng.switch(self.h1, fade_len=0)
        self.assertEqual(eng.active_layer_count(0), 1)
        np.testing.assert_allclose(eng.layer_gains(0), [1.0])

    def test_after_fade_output_equals_new_filter_convolution(self):
        # 渐变结束后,剩余层即新滤波器;之后输出必须等于新滤波器的
        # 整段卷积(切换前输入仍在其延迟线中,经 prime 正确带入)
        fade = 128
        eng = StreamingFIREngine(self.h0)
        eng.process(self.x[:self.at])
        eng.switch(self.h1, fade_len=fade)
        rest = self.x[self.at:]
        eng.process(rest[:fade])  # 渐变结束
        self.assertEqual(eng.active_layer_count(0), 1)
        y_after = eng.process(rest[fade:])
        # 与“从头就使用 h1”的整段卷积在同一段上对比
        full = reference_full_convolution(self.x, self.h1)
        seg = full[self.at + fade:self.x.size]
        np.testing.assert_allclose(y_after, seg, atol=1e-11)

    def test_new_filter_no_warmup_transient(self):
        # 新层经 prime,切换后第一个样本就不应有预热瞬态:
        # 与逐样本参考在 fade 内第一段比对(独立实现)
        fade = 64
        y = process_with_switch_at(
            self.x, self.h0, self.h1, self.at, fade, StreamingFIREngine)
        yref = process_with_switch_at(
            self.x, self.h0, self.h1, self.at, fade, LayeredReferenceFIR)
        # 重点检查渐变前 8 个样本
        np.testing.assert_allclose(
            y[self.at:self.at + 8], yref[self.at:self.at + 8], atol=1e-12)

    def test_different_filter_lengths(self):
        # 切换到更短和更长的滤波器都正常
        h_short = filters.identity()
        h_long = filters.lowpass(500, 129, self.sr)
        for h1 in (h_short, h_long):
            y = process_with_switch_at(
                self.x, self.h0, h1, self.at, 200, StreamingFIREngine)
            yref = process_with_switch_at(
                self.x, self.h0, h1, self.at, 200, LayeredReferenceFIR)
            np.testing.assert_allclose(y, yref, atol=1e-10)


if __name__ == "__main__":
    unittest.main()
