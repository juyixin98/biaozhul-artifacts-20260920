"""pcm.pcm:16/24 bit 整数 PCM 与浮点振幅转换测试。"""

from __future__ import annotations

import numpy as np
import unittest

from pcmval import pcm


class TestPCM16(unittest.TestCase):
    def test_decode_known_values(self):
        # 小端序 + 有符号
        raw = (0x80).to_bytes(1, "little") + (0x00).to_bytes(1, "little")  # +128
        raw += (0xFF).to_bytes(1, "little") + (0x7F).to_bytes(1, "little")  # +32767
        raw += (0x00).to_bytes(1, "little") + (0x80).to_bytes(1, "little")  # -32768
        decoded = pcm.decode_pcm16(raw)
        np.testing.assert_array_equal(decoded, [128, 32767, -32768])

    def test_extreme_amplitude_mapping(self):
        samples = np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)
        amp = pcm.dequantize(samples, 16)
        self.assertEqual(amp[0], -1.0)
        self.assertEqual(amp[-1], 32767 / 32768)
        self.assertEqual(amp[2], 0.0)
        # 往返:整数 -> 振幅 -> 整数,所有极值完全还原
        back = pcm.quantize(amp, 16)
        np.testing.assert_array_equal(back, samples)

    def test_byte_roundtrip(self):
        samples = np.linspace(-32768, 32767, 101).round().astype(np.int16)
        blob = pcm.encode_pcm16(samples)
        self.assertEqual(len(blob), 202)
        np.testing.assert_array_equal(pcm.decode_pcm16(blob), samples)

    def test_odd_stream_length_rejected(self):
        with self.assertRaises(ValueError):
            pcm.decode_pcm16(b"\x00")

    def test_quantize_half_lsb(self):
        # 0.5 LSB 阈值点的舍入(round half to even)
        lsb = 1 / 32768
        samples = pcm.quantize(np.array([0.5 * lsb, -0.5 * lsb, 1.5 * lsb]), 16)
        np.testing.assert_array_equal(samples, [0, 0, 2])


class TestPCM24(unittest.TestCase):
    def test_decode_known_values(self):
        cases = [
            (0x7FFFFF, bytes((0xFF, 0xFF, 0x7F))),   # +8388607
            (-8388608, bytes((0x00, 0x00, 0x80))),   # -8388608
            (-1, bytes((0xFF, 0xFF, 0xFF))),
            (1, bytes((0x01, 0x00, 0x00))),
            (196, bytes((0xC4, 0x00, 0x00))),
        ]
        raw = b"".join(c[1] for c in cases)
        decoded = pcm.decode_pcm24(raw)
        np.testing.assert_array_equal(decoded, [c[0] for c in cases])

    def test_extreme_amplitude_mapping(self):
        samples = np.array([-8388608, -1, 0, 1, 8388607], dtype=np.int32)
        amp = pcm.dequantize(samples, 24)
        self.assertEqual(amp[0], -1.0)
        self.assertEqual(amp[-1], 8388607 / 8388608)
        back = pcm.quantize(amp, 24)
        np.testing.assert_array_equal(back, samples)

    def test_byte_roundtrip(self):
        samples = np.linspace(-8388608, 8388607, 257).round().astype(np.int32)
        blob = pcm.encode_pcm24(samples)
        self.assertEqual(len(blob), 257 * 3)
        np.testing.assert_array_equal(pcm.decode_pcm24(blob), samples)

    def test_non_multiple_of_three_rejected(self):
        with self.assertRaises(ValueError):
            pcm.decode_pcm24(b"\x00\x00")

    def test_encode_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            pcm.encode_pcm24(np.array([8388608], dtype=np.int32))
        with self.assertRaises(ValueError):
            pcm.encode_pcm24(np.array([-8388609], dtype=np.int32))


class TestGenericAndRejection(unittest.TestCase):
    def test_unsupported_bits(self):
        for bits in (8, 12, 32, 20):
            with self.assertRaises(pcm.UnsupportedBitDepthError):
                pcm.int_dtype(bits)
            with self.assertRaises(pcm.UnsupportedBitDepthError):
                pcm.decode_pcm(b"", bits)
            with self.assertRaises(pcm.UnsupportedBitDepthError):
                pcm.quantize(np.array([0.0]), bits)

    def test_clipping_on_quantize(self):
        # +1.0 超过 int16 正满幅 -> 裁剪到 +32767(不溢出为 -32768)
        out = pcm.quantize(np.array([1.0, -1.5, 1.5]), 16)
        np.testing.assert_array_equal(out, [32767, -32768, 32767])
        out24 = pcm.quantize(np.array([1.0, -1.0]), 24)
        np.testing.assert_array_equal(out24, [8388607, -8388608])

    def test_non_finite_rejected(self):
        with self.assertRaises(ValueError):
            pcm.quantize(np.array([np.nan]), 16)
        with self.assertRaises(ValueError):
            pcm.quantize(np.array([np.inf]), 24)

    def test_int_ranges(self):
        self.assertEqual(pcm.int_range(16), (-32768, 32767))
        self.assertEqual(pcm.int_range(24), (-8388608, 8388607))


if __name__ == "__main__":
    unittest.main(verbosity=2)
