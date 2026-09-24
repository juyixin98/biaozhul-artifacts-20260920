"""pcm.wavio:WAV 容器读写、未知 chunk 跳过、补齐、截断与格式拒绝测试。"""

from __future__ import annotations

import struct
import unittest
import wave
import io

import numpy as np

from pcmval import pcm, wavio
from pcmval.wavio import WavFormatError

from .helpers import build_wav, fmt_payload, pcm_frames, raw_chunk


def issue_codes(result):
    return [i.code for i in result.issues]


class TestRoundtrip(unittest.TestCase):
    def test_16bit_simple(self):
        samples = np.array([0, 1, -1, 32767, -32768, 12345], dtype=np.int16)
        blob = wavio.encode_wav(samples, 8000, 1, 16)
        r = wavio.read_wav(blob)
        self.assertEqual(r.sample_rate, 8000)
        self.assertEqual(r.channels, 1)
        self.assertEqual(r.bits_per_sample, 16)
        self.assertEqual(r.frames, 6)
        np.testing.assert_array_equal(r.samples, samples)
        self.assertEqual(r.issues, [])

    def test_24bit_stereo(self):
        samples = np.array(
            [[-8388608, 8388607], [0, -1], [42, 196]], dtype=np.int32
        )
        blob = wavio.encode_wav(samples, 48000, 2, 24)
        r = wavio.read_wav(blob)
        self.assertEqual(r.channels, 2)
        self.assertEqual(r.bits_per_sample, 24)
        self.assertEqual(r.frames, 3)
        self.assertEqual(r.samples.shape, (3, 2))
        np.testing.assert_array_equal(r.samples, samples)
        # fmt 头字段
        self.assertEqual(r.sample_rate, 48000)

    def test_riff_size_covers_pad(self):
        # data 长度 5 字节(奇数)-> 应有 1 字节 pad,RIFF 长度计入 pad
        odd_payload = b"\x01\x02\x03\x04\x05"
        blob = build_wav(
            [raw_chunk(b"fmt ", fmt_payload(bits=16, channels=1)),
             raw_chunk(b"data", odd_payload)]
        )
        riff_size = struct.unpack("<I", blob[4:8])[0]
        self.assertEqual(riff_size, len(blob) - 8)
        # 5 字节 -> 4 字节可用(2 样本)+ PARTIAL_FRAME 警告
        r = wavio.read_wav(blob)
        self.assertIn("PARTIAL_FRAME", issue_codes(r))
        self.assertEqual(r.frames, 2)

    def test_stdriff_wave_module_interop(self):
        """标准库 wave 写出的 16bit WAV 必须能被本模块读回。"""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(11025)
            frames = struct.pack("<4h", 100, -100, 32767, -32768)
            w.writeframes(frames)
        r = wavio.read_wav(buf.getvalue())
        self.assertEqual((r.channels, r.bits_per_sample, r.frames), (2, 16, 2))
        np.testing.assert_array_equal(r.samples, [[100, -100], [32767, -32768]])

        # 反向:本模块写的,wave 也能读
        blob = wavio.encode_wav(np.array([1, 2, 3, 4], np.int16), 11025, 2, 16)
        with wave.open(io.BytesIO(blob), "rb") as w:
            self.assertEqual(w.getnchannels(), 2)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.readframes(w.getnframes()), struct.pack("<4h", 1, 2, 3, 4))


class TestUnknownChunks(unittest.TestCase):
    def _file_with_junk(self, junk_len: int, bits: int = 16):
        junk = bytes((i * 7) % 256 for i in range(junk_len))
        frames = pcm_frames(10, -10, bits=bits)
        return build_wav([
            raw_chunk(b"JUNK", junk),
            raw_chunk(b"fmt ", fmt_payload(bits=bits)),
            raw_chunk(b"LIST", b"\x00" * 3),          # 奇数负载 + pad
            raw_chunk(b"id3 ", b"tag-data"),          # 8 字节偶数
            raw_chunk(b"data", frames),
        ])

    def test_skip_even_length_junk_16(self):
        blob = self._file_with_junk(12)
        r = wavio.read_wav(blob)
        np.testing.assert_array_equal(r.samples, [10, -10])
        ids = [c.id for c in r.chunks]
        self.assertEqual(ids, ["JUNK", "fmt ", "LIST", "id3 ", "data"])
        skipped = [c for c in r.chunks if c.handled == "skipped"]
        self.assertEqual(len(skipped), 3)

    def test_skip_odd_length_junk_24(self):
        # 奇数长度 JUNK / LIST 的 pad 必须被正确跳过,否则 fmt/data 解析错位
        blob = self._file_with_junk(13, bits=24)
        r = wavio.read_wav(blob)
        self.assertEqual(r.bits_per_sample, 24)
        np.testing.assert_array_equal(r.samples, [10, -10])
        list_chunk = next(c for c in r.chunks if c.id == "LIST")
        self.assertEqual(list_chunk.size, 3)
        self.assertEqual(list_chunk.padded_size, 4)

    def test_chunk_after_odd_data_with_pad(self):
        # data 本身奇数长度时,其后的 chunk 也必须能定位
        tail = raw_chunk(b"LIST", b"ab")  # data 之后
        blob = build_wav([
            raw_chunk(b"fmt ", fmt_payload(bits=16)),
            raw_chunk(b"data", b"\x00\x01\x02"),  # 3 字节 -> pad
            tail,
        ])
        r = wavio.read_wav(blob)
        self.assertEqual([c.id for c in r.chunks], ["fmt ", "data", "LIST"])
        self.assertIn("PARTIAL_FRAME", issue_codes(r))


class TestTruncation(unittest.TestCase):
    def test_truncated_file_header(self):
        with self.assertRaises(WavFormatError):
            wavio.read_wav(b"RIFF\x24\x00")

    def test_partial_chunk_header_treated_as_trailing_garbage(self):
        # fmt 之后只剩 2 字节,不够一个 chunk 头 -> 尾部垃圾告警 + 缺 data 报错
        blob = build_wav([raw_chunk(b"fmt ", fmt_payload())])
        blob += b"da"
        with self.assertRaisesRegex(WavFormatError, "missing data"):
            wavio.read_wav(blob)

    def test_trailing_garbage_after_valid_file_is_warning(self):
        blob = wavio.encode_wav(np.array([1, 2], np.int16), 8000, 1, 16)
        blob += b"\xDE\xAD\xBE"  # 3 字节垃圾
        r = wavio.read_wav(blob)
        self.assertIn("TRAILING_BYTES", issue_codes(r))
        self.assertEqual(r.frames, 2)

    def test_truncated_chunk_payload(self):
        # fmt 声明 16 字节但只放了 10 字节
        blob = build_wav([
            raw_chunk(b"fmt ", fmt_payload()[:10], declared_size=16),
        ])
        with self.assertRaisesRegex(WavFormatError, r"truncated chunk .fmt."):
            wavio.read_wav(blob)

    def test_truncated_unknown_chunk_is_not_silently_skipped(self):
        # JUNK 声明 100 字节但文件只给到 20:截断必须报错,不能假装跳过
        blob = build_wav([
            raw_chunk(b"JUNK", b"\x00" * 20, declared_size=100),
            raw_chunk(b"fmt ", fmt_payload()),
            raw_chunk(b"data", pcm_frames(1, 2)),
        ])
        with self.assertRaisesRegex(WavFormatError, r"truncated chunk .JUNK."):
            wavio.read_wav(blob)

    def test_truncated_data_chunk(self):
        blob = build_wav([
            raw_chunk(b"fmt ", fmt_payload()),
            raw_chunk(b"data", pcm_frames(1, 2, 3)[:4], declared_size=6),
        ])
        with self.assertRaisesRegex(WavFormatError, r"truncated chunk .data."):
            wavio.read_wav(blob)


class TestRIFFLengthAndAlignment(unittest.TestCase):
    def test_riff_size_larger_than_file(self):
        blob = wavio.encode_wav(np.array([1, 2], np.int16), 8000, 1, 16)
        blob = blob[:-2]  # 砍掉 2 字节样本
        # RIFF 声明长度 > 实际长度:warning;data 同时截断 -> error
        with self.assertRaises(WavFormatError):
            wavio.read_wav(blob)

    def test_riff_size_declares_extra_trailing_bytes(self):
        # 文件实际比 RIFF 声明多 4 字节尾巴 -> warning 但正常解析
        blob = wavio.encode_wav(np.array([1, 2], np.int16), 8000, 1, 16)
        blob += b"\x00\x00\x00\x00"
        r = wavio.read_wav(blob)
        self.assertIn("RIFF_SIZE_MISMATCH", issue_codes(r))
        self.assertEqual(r.frames, 2)
        with self.assertRaises(WavFormatError):
            wavio.read_wav(blob, strict=True)

    def test_partial_frame_warning_16(self):
        # 3 字节 data / 2 字节帧 -> 1 个完整样本 + 残缺 1 字节
        blob = build_wav([
            raw_chunk(b"fmt ", fmt_payload(bits=16, channels=1)),
            raw_chunk(b"data", b"\x01\x00\x02"),
        ])
        r = wavio.read_wav(blob)
        self.assertEqual(r.data_size, 3)
        self.assertEqual(r.frames, 1)
        self.assertIn("PARTIAL_FRAME", issue_codes(r))

    def test_partial_frame_stereo_24(self):
        # 帧 = 2ch*3B = 6B;7 字节 -> 1 帧 + 1 残字节
        good = pcm_frames(100, 200, bits=24)  # 6 字节
        blob = build_wav([
            raw_chunk(b"fmt ", fmt_payload(bits=24, channels=2)),
            raw_chunk(b"data", good + b"\x99"),
        ])
        r = wavio.read_wav(blob)
        self.assertEqual(r.frames, 1)
        np.testing.assert_array_equal(r.samples, [[100, 200]])


class TestFormatRejection(unittest.TestCase):
    def _wav_with_fmt(self, payload: bytes) -> bytes:
        return build_wav([
            raw_chunk(b"fmt ", payload),
            raw_chunk(b"data", b""),
        ])

    def test_not_riff(self):
        with self.assertRaisesRegex(WavFormatError, "not a RIFF"):
            wavio.read_wav(b"RF64" + b"\x00" * 40)

    def test_not_wave(self):
        blob = build_wav([raw_chunk(b"fmt ", fmt_payload())], form=b"AVI ")
        with self.assertRaisesRegex(WavFormatError, "not a WAVE"):
            wavio.read_wav(blob)

    def test_missing_fmt(self):
        blob = build_wav([raw_chunk(b"data", pcm_frames(1, 2))])
        with self.assertRaisesRegex(WavFormatError, "missing fmt"):
            wavio.read_wav(blob)

    def test_missing_data(self):
        blob = build_wav([raw_chunk(b"fmt ", fmt_payload())])
        with self.assertRaisesRegex(WavFormatError, "missing data"):
            wavio.read_wav(blob)

    def test_8bit_rejected(self):
        with self.assertRaisesRegex(WavFormatError, "unsupported bit depth 8"):
            wavio.read_wav(self._wav_with_fmt(fmt_payload(bits=8)))

    def test_32bit_rejected(self):
        with self.assertRaisesRegex(WavFormatError, "unsupported bit depth 32"):
            wavio.read_wav(self._wav_with_fmt(fmt_payload(bits=32)))

    def test_float_format_rejected(self):
        # WAVE_FORMAT_IEEE_FLOAT = 0x0003
        with self.assertRaisesRegex(WavFormatError, "0x0003"):
            wavio.read_wav(self._wav_with_fmt(fmt_payload(bits=32, tag=0x0003)))

    def test_extensible_pcm_accepted(self):
        blob = build_wav([
            raw_chunk(b"fmt ", fmt_payload(bits=24, tag=0xFFFE)),
            raw_chunk(b"data", pcm_frames(-8388608, 8388607, bits=24)),
        ])
        r = wavio.read_wav(blob)
        self.assertEqual(r.format_tag, wavio.WAVE_FORMAT_PCM)
        np.testing.assert_array_equal(r.samples, [-8388608, 8388607])

    def test_extensible_float_guid_rejected(self):
        # IEEE_FLOAT 子格式 GUID:00000003-0000-0010-8000-00aa00389b71
        payload = bytearray(fmt_payload(bits=32, tag=0xFFFE))
        payload[24:26] = struct.pack("<H", 3)
        with self.assertRaisesRegex(WavFormatError, "sub-format GUID"):
            wavio.read_wav(self._wav_with_fmt(bytes(payload)))

    def test_block_align_mismatch_rejected(self):
        payload = fmt_payload(bits=16, channels=2, block_align=3)
        with self.assertRaisesRegex(WavFormatError, "block_align mismatch"):
            wavio.read_wav(self._wav_with_fmt(payload))

    def test_byte_rate_mismatch_is_warning(self):
        payload = fmt_payload(bits=16, channels=1, byte_rate=12345)
        blob = build_wav([
            raw_chunk(b"fmt ", payload),
            raw_chunk(b"data", pcm_frames(1)),
        ])
        r = wavio.read_wav(blob)
        self.assertIn("BYTE_RATE_MISMATCH", issue_codes(r))
        self.assertEqual(r.frames, 1)


class TestWrite(unittest.TestCase):
    def test_write_float_inputs_quantized(self):
        blob = wavio.encode_wav(pcm.quantize(np.array([-1.0, 0.0, 32767 / 32768]), 16),
                                8000, 1, 16)
        r = wavio.read_wav(blob)
        np.testing.assert_array_equal(r.samples, [-32768, 0, 32767])

    def test_encode_rejects_bad_bits(self):
        with self.assertRaises(pcm.UnsupportedBitDepthError):
            wavio.encode_wav(np.array([1], np.int8), 8000, 1, 8)

    def test_channel_count_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            wavio.encode_wav(np.zeros((3, 2), np.int16), 8000, channels=1, bits=16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
