"""验收测试:题目明确要求的 5 类场景。

1. 截断 chunk            -> 结构性拒绝,错误信息指向具体 chunk/偏移
2. 错误采样对齐          -> PARTIAL_FRAME,残缺字节丢弃,完整帧保留
3. 极值样本(16/24 bit)  -> 满幅整数与 [-1,1) 振幅精确互转
4. 往返字节              -> PCM 负载字节、(干净文件)整文件字节一致
5. 格式拒绝              -> 8bit/32bit/IEEE float 均拒绝
"""

from __future__ import annotations

import struct
import unittest

import numpy as np

from pcmval import pcm, wavio
from pcmval.wavio import WavFormatError

from .helpers import build_wav, fmt_payload, pcm_frames, raw_chunk


class TestAcceptance(unittest.TestCase):

    # ---- 场景 1:截断 chunk ------------------------------------------------
    def test_accept_1_truncated_chunks(self):
        # 1a. 未知 chunk 负载截断:不得静默跳过
        truncated_junk = build_wav([
            raw_chunk(b"JUNK", b"\x00" * 4, declared_size=64),
            raw_chunk(b"fmt ", fmt_payload()),
            raw_chunk(b"data", pcm_frames(1)),
        ])
        with self.assertRaisesRegex(WavFormatError, r"JUNK.*truncated|truncated.*JUNK"):
            wavio.read_wav(truncated_junk)

        # 1b. 必需 chunk(fmt)负载截断
        truncated_fmt = build_wav([
            raw_chunk(b"fmt ", fmt_payload()[:8], declared_size=16),
        ])
        with self.assertRaisesRegex(WavFormatError, r"fmt .*truncated|truncated.*fmt"):
            wavio.read_wav(truncated_fmt)

        # 1c. chunk 头本身截断(文件在 8 字节头中间结束)
        truncated_header = build_wav([
            raw_chunk(b"fmt ", fmt_payload()),
            raw_chunk(b"data", pcm_frames(1, 2)),
        ])[:-6]  # 删掉 data 的部分头/负载
        with self.assertRaises(WavFormatError):
            wavio.read_wav(truncated_header)

    # ---- 场景 2:错误采样对齐 --------------------------------------------
    def test_accept_2_wrong_sample_alignment(self):
        # 16bit 单声道:帧=2B,data=5B -> 2 个完整样本 + 1 残字节
        blob16 = build_wav([
            raw_chunk(b"fmt ", fmt_payload(bits=16, channels=1, sample_rate=8000)),
            raw_chunk(b"data", pcm_frames(-100, 100) + b"\x55"),
        ])
        r = wavio.read_wav(blob16)
        self.assertEqual(r.data_size, 5)
        self.assertEqual(r.frames, 2)
        np.testing.assert_array_equal(r.samples, [-100, 100])
        codes = [i.code for i in r.issues]
        self.assertIn("PARTIAL_FRAME", codes)
        messages = " | ".join(i.message for i in r.issues)
        self.assertIn("dropped 1 trailing byte", messages)

        # 24bit 立体声:帧=6B,data=8B -> 1 帧 + 2 残字节
        blob24 = build_wav([
            raw_chunk(b"fmt ", fmt_payload(bits=24, channels=2, sample_rate=48000)),
            raw_chunk(b"data", pcm_frames(-8388608, 8388607, bits=24) + b"\x00\x00"),
        ])
        r24 = wavio.read_wav(blob24)
        self.assertEqual(r24.frames, 1)
        np.testing.assert_array_equal(r24.samples, [[-8388608, 8388607]])
        self.assertIn("PARTIAL_FRAME", [i.code for i in r24.issues])

    # ---- 场景 3:极值样本与振幅转换 ---------------------------------------
    def test_accept_3_extreme_samples(self):
        for bits, lo, hi in (
            (16, -32768, 32767),
            (24, -8388608, 8388607),
        ):
            with self.subTest(bits=bits):
                ints = np.array([lo, 0, hi], dtype=np.int32)
                amp = pcm.dequantize(ints, bits)
                self.assertEqual(amp[0], -1.0, f"{bits}bit 负满幅必须是 -1.0")
                self.assertEqual(amp[1], 0.0)
                self.assertEqual(amp[-1], hi / (abs(lo)), f"{bits}bit 正满幅 = 2^-(b-1) 余量")
                back = pcm.quantize(amp, bits)
                np.testing.assert_array_equal(back, ints)

                # 经字节编解码后仍然是同一个极值(符号扩展正确)
                decoded = pcm.decode_pcm(pcm.encode_pcm(ints, bits), bits)
                np.testing.assert_array_equal(decoded, ints)

        # +1.0 量化在 16/24bit 都必须裁剪到正满幅,绝不翻转成负值
        np.testing.assert_array_equal(pcm.quantize(np.array([1.0]), 16), [32767])
        np.testing.assert_array_equal(pcm.quantize(np.array([1.0]), 24), [8388607])

    # ---- 场景 4:往返字节 -------------------------------------------------
    def test_accept_4_roundtrip_bytes(self):
        for bits, lo, hi in (
            (16, -32768, 32767),
            (24, -8388608, 8388607),
        ):
            with self.subTest(bits=bits):
                ints = np.array([lo, -1, 0, 1, hi, 196, -196], dtype=np.int32)
                ints = ints.astype(pcm.int_dtype(bits))
                blob = wavio.encode_wav(ints, 8000, 1, bits)
                r = wavio.read_wav(blob)

                # 4a. PCM 负载逐字节一致
                payload = blob[r.data_offset:r.data_offset + r.data_size]
                self.assertEqual(payload, pcm.encode_pcm(ints, bits))
                np.testing.assert_array_equal(r.samples, ints)

                # 4b. 整文件再写一次(干净无子 chunk)字节一致
                self.assertEqual(
                    wavio.encode_wav(r.samples, r.sample_rate, r.channels,
                                     r.bits_per_sample),
                    blob,
                )

                # 4c. RIFF 长度字段与文件实际长度一致,且包含奇数 pad 规则
                riff_size = struct.unpack("<I", blob[4:8])[0]
                self.assertEqual(8 + riff_size, len(blob))

        # 4d. 奇数长度未知 chunk 的 pad 不影响后续 data 往返
        blob = build_wav([
            raw_chunk(b"JUNK", b"\x01\x02\x03"),            # 3 字节 + pad
            raw_chunk(b"fmt ", fmt_payload(bits=24)),
            raw_chunk(b"data", pcm_frames(-55, 55, bits=24)),
        ])
        r = wavio.read_wav(blob)
        np.testing.assert_array_equal(r.samples, [-55, 55])
        self.assertEqual(len([c for c in r.chunks if c.handled == "skipped"]), 1)

    # ---- 场景 5:其他格式拒绝 ---------------------------------------------
    def test_accept_5_reject_non_pcm_formats(self):
        cases = [
            ("8-bit PCM", fmt_payload(bits=8, tag=0x0001)),
            ("32-bit PCM", fmt_payload(bits=32, tag=0x0001)),
            ("32-bit IEEE float", fmt_payload(bits=32, tag=0x0003)),
            ("64-bit float", fmt_payload(bits=64, tag=0x0003)),
        ]
        for label, fmt in cases:
            with self.subTest(fmt=label):
                blob = build_wav([
                    raw_chunk(b"fmt ", fmt),
                    raw_chunk(b"data", b"\x00" * 16),
                ])
                with self.assertRaises(WavFormatError):
                    wavio.read_wav(blob)

        # 非 RIFF / 非 WAVE 也拒绝
        with self.assertRaises(WavFormatError):
            wavio.read_wav(b"AIFF" + b"\x00" * 40)
        blob = build_wav([raw_chunk(b"fmt ", fmt_payload())], form=b"AVI ")
        with self.assertRaises(WavFormatError):
            wavio.read_wav(blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
