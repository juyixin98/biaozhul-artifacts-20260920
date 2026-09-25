"""WAV PCM 子集读写测试：截断 chunk、对齐错误、极值样本、字节往返、
奇数字节补齐、RIFF 长度、未知 chunk 跳过、格式拒绝。"""

import struct
import unittest

import numpy as np

from pcm_container import wavio
from pcm_container.amplitude import from_float, int_max, int_min, to_float
from pcm_container.errors import (
    PcmAlignmentError,
    WavFormatError,
    WavTruncatedError,
)


def fmt_body(channels=1, sample_rate=48000, bits=16, tag=1):
    block = channels * bits // 8
    return struct.pack(
        "<HHIIHH", tag, channels, sample_rate, sample_rate * block, block, bits
    )


def chunk(cid: bytes, body: bytes, pad_byte: bool | None = None) -> bytes:
    """组装 chunk。pad_byte: None=自动(奇补00)；False=故意不补；True=补00。"""
    out = cid + struct.pack("<I", len(body)) + body
    if pad_byte is None:
        pad_byte = bool(len(body) & 1)
    if pad_byte:
        out += b"\x00"
    return out


def riff(payload: bytes, declared_size: int | None = None) -> bytes:
    if declared_size is None:
        declared_size = len(payload) + 4
    return b"RIFF" + struct.pack("<I", declared_size) + payload


def make_wav(
    samples: np.ndarray,
    channels=1,
    sample_rate=48000,
    bits=16,
    extra: list | None = None,
):
    """走正规写出器构造合法文件。"""
    return wavio.build_wav_bytes(samples, sample_rate, channels, bits, extra)


def encode_interleaved(samples: np.ndarray, bits: int) -> bytes:
    if bits == 16:
        return np.asarray(samples, dtype="<i2").tobytes()
    flat = np.asarray(samples, dtype=np.int32).reshape(-1)
    u = flat.astype(np.uint32) & 0xFFFFFF
    b = np.empty((flat.size, 3), dtype=np.uint8)
    b[:, 0] = u & 0xFF
    b[:, 1] = (u >> 8) & 0xFF
    b[:, 2] = (u >> 16) & 0xFF
    return b.tobytes()


class TestRoundtrip(unittest.TestCase):
    def test_bytes_identical_roundtrip_16(self):
        rng = np.random.default_rng(7)
        codes = rng.integers(-1000, 1000, size=1024, dtype=np.int32)
        blob = make_wav(codes, sample_rate=44100, bits=16)
        parsed = wavio.parse_wav(blob)
        np.testing.assert_array_equal(parsed.samples.reshape(-1), codes)
        # 再次写出应与原始字节逐字节相同
        blob2 = wavio.build_wav_bytes(
            parsed.samples, 44100, parsed.channels, parsed.bits
        )
        self.assertEqual(blob, blob2)

    def test_bytes_identical_roundtrip_24_stereo(self):
        rng = np.random.default_rng(8)
        codes = rng.integers(-100000, 100000, size=(512, 2), dtype=np.int32)
        blob = make_wav(codes, channels=2, sample_rate=96000, bits=24)
        parsed = wavio.parse_wav(blob)
        self.assertEqual(parsed.samples.shape, (512, 2))
        blob2 = wavio.build_wav_bytes(
            parsed.samples, 96000, 2, 24
        )
        self.assertEqual(blob, blob2)

    def test_roundtrip_preserves_unknown_chunks_when_rebuilt(self):
        codes = np.array([0, 100, -100], dtype=np.int32)
        extra = [("labl", b"abc"), ("JUNK", b"\x00" * 10)]
        blob = make_wav(codes, extra=extra)
        parsed = wavio.parse_wav(blob)
        self.assertEqual([c[0] for c in parsed.extra_chunks], ["labl", "JUNK"])
        rebuilt = wavio.build_wav_bytes(
            parsed.samples, 48000, 1, 16, parsed.extra_chunks
        )
        self.assertEqual(blob, rebuilt)


class TestExtremeSamples(unittest.TestCase):
    def test_extreme_codes_16(self):
        codes = np.array([int_min(16), 0, int_max(16)], dtype=np.int32)
        parsed = wavio.parse_wav(make_wav(codes, bits=16))
        np.testing.assert_array_equal(parsed.samples.reshape(-1), codes)
        amp = to_float(parsed.samples, 16).reshape(-1)
        self.assertEqual(amp[0], -1.0)
        self.assertEqual(amp[-1], 32767 / 32768)
        # 从振幅极限反向重建不越界、可往返
        rebuilt = from_float(np.array([-1.0, 1.0]), 16)
        self.assertEqual(rebuilt.tolist(), [-32768, 32767])

    def test_extreme_codes_24(self):
        codes = np.array(
            [int_min(24), -1, 0, 1, int_max(24)], dtype=np.int32
        )
        parsed = wavio.parse_wav(make_wav(codes, bits=24))
        np.testing.assert_array_equal(parsed.samples.reshape(-1), codes)
        self.assertEqual(to_float(parsed.samples[:1], 24)[0, 0], -1.0)
        rebuilt = from_float(np.array([-1.0, 1.0]), 24)
        self.assertEqual(rebuilt.tolist(), [-8388608, 8388607])

    def test_write_rejects_out_of_domain(self):
        bad = np.array([int_max(16) + 1], dtype=np.int32)
        with self.assertRaises(ValueError):
            wavio.build_wav_bytes(bad, 48000, 1, 16)
        bad2 = np.array([int_min(24) - 1], dtype=np.int32)
        with self.assertRaises(ValueError):
            wavio.build_wav_bytes(bad2, 48000, 1, 24)

    def test_24bit_negative_encoding_bytes(self):
        codes = np.array([-1, -2, -8388608], dtype=np.int32)
        blob = make_wav(codes, bits=24)
        parsed = wavio.parse_wav(blob)
        np.testing.assert_array_equal(parsed.samples.reshape(-1), codes)


class TestTruncation(unittest.TestCase):
    def _base_chunks(self):
        data = encode_interleaved(np.zeros(4, dtype=np.int32), 16)
        return chunk(b"fmt ", fmt_body()) + chunk(b"data", data)

    def test_header_too_short(self):
        with self.assertRaises(WavTruncatedError):
            wavio.parse_wav(b"RIFF\x10\x00")

    def test_chunk_header_cut(self):
        # fmt/data 之后追加半个 chunk 头
        blob = riff(b"WAVE" + self._base_chunks() + b"JUN")
        with self.assertRaises(WavTruncatedError):
            wavio.parse_wav(blob)

    def test_chunk_body_cut_unknown(self):
        # 截断的未知 chunk：声明 5 字节，只给 2 字节
        evil = b"JUNK" + struct.pack("<I", 5) + b"\x00\x00"
        blob = riff(b"WAVE" + self._base_chunks() + evil)
        with self.assertRaises(WavTruncatedError):
            wavio.parse_wav(blob)

    def test_data_chunk_body_cut(self):
        data = encode_interleaved(np.zeros(4, dtype=np.int32), 16)
        evil = b"data" + struct.pack("<I", len(data)) + data[:-3]
        blob = riff(b"WAVE" + chunk(b"fmt ", fmt_body()) + evil)
        with self.assertRaises(WavTruncatedError):
            wavio.parse_wav(blob)

    def test_missing_pad_byte_inside_container(self):
        # 奇数尺寸的未知 chunk 故意不写 pad，但后面还有 data chunk
        evil = chunk(b"JUNK", b"abc", pad_byte=False)
        data = encode_interleaved(np.zeros(2, dtype=np.int32), 16)
        blob = riff(
            b"WAVE"
            + chunk(b"fmt ", fmt_body())
            + evil
            + chunk(b"data", data)
        )
        with self.assertRaises(WavTruncatedError):
            wavio.parse_wav(blob)

    def test_missing_pad_byte_at_eof_tolerated(self):
        # data 为奇数长度（24 位单声道 1 帧 = 3 字节），文件尾缺 pad -> 宽容接受
        data = encode_interleaved(np.array([42], dtype=np.int32), 24)
        self.assertEqual(len(data) & 1, 1)
        body = chunk(b"fmt ", fmt_body(bits=24)) + chunk(
            b"data", data, pad_byte=False
        )
        blob = riff(b"WAVE" + body)
        parsed = wavio.parse_wav(blob)
        self.assertEqual(parsed.samples[0, 0], 42)

    def test_odd_chunk_with_pad_skipped(self):
        data = encode_interleaved(np.array([1, -1], dtype=np.int32), 16)
        body = (
            chunk(b"fmt ", fmt_body())
            + chunk(b"JUNK", b"abc")  # 3 字节，自动补 pad
            + chunk(b"data", data)
        )
        blob = riff(b"WAVE" + body)
        parsed = wavio.parse_wav(blob)
        np.testing.assert_array_equal(parsed.samples.reshape(-1), [1, -1])
        self.assertEqual(parsed.extra_chunks[0], ("JUNK", b"abc"))


class TestAlignment(unittest.TestCase):
    def test_data_not_multiple_of_block_align(self):
        # 16 位单声道：data 多 1 字节
        data = encode_interleaved(np.zeros(3, dtype=np.int32), 16) + b"\x00"
        body = chunk(b"fmt ", fmt_body()) + chunk(b"data", data)
        with self.assertRaises(PcmAlignmentError):
            wavio.parse_wav(riff(b"WAVE" + body))

    def test_stereo_data_not_frame_aligned(self):
        # 16 位立体声：block align=4，data 长度 6 -> 对不齐
        data = b"\x00" * 6
        body = chunk(b"fmt ", fmt_body(channels=2)) + chunk(b"data", data)
        with self.assertRaises(PcmAlignmentError):
            wavio.parse_wav(riff(b"WAVE" + body))

    def test_block_align_contradiction(self):
        body = chunk(b"fmt ", fmt_body(channels=2)) + chunk(
            b"data", encode_interleaved(np.zeros(4, dtype=np.int32), 16)
        )
        # 手工把 fmt 里的 block align 改成 3
        bad_fmt = fmt_body(channels=2)
        bad_fmt = bad_fmt[:12] + struct.pack("<H", 3) + bad_fmt[14:]
        body = chunk(b"fmt ", bad_fmt) + chunk(
            b"data", encode_interleaved(np.zeros((2, 2), dtype=np.int32), 16)
        )
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"WAVE" + body))


class TestRiffSize(unittest.TestCase):
    def test_riff_size_flagged_when_wrong(self):
        codes = np.array([1, 2, 3], dtype=np.int32)
        blob = bytearray(make_wav(codes))
        struct.pack_into("<I", blob, 4, 999)  # 篡改 RIFF 声明尺寸
        parsed = wavio.parse_wav(bytes(blob))
        self.assertFalse(parsed.riff_size_matches_file)
        self.assertEqual(parsed.riff_size, 999)
        self.assertEqual(parsed.actual_payload, len(blob) - 8)

    def test_riff_size_correct_on_write(self):
        codes = np.arange(10, dtype=np.int32)
        blob = make_wav(codes)
        declared = struct.unpack_from("<I", blob, 4)[0]
        self.assertEqual(declared, len(blob) - 8)
        parsed = wavio.parse_wav(blob)
        self.assertTrue(parsed.riff_size_matches_file)

    def test_riff_size_tolerates_file_trailing_bytes(self):
        # RIFF 声明正确但文件尾部多 2 个杂散字节：不报错，仅标记不匹配
        codes = np.array([1], dtype=np.int32)
        blob = make_wav(codes) + b"\x00\x00"
        parsed = wavio.parse_wav(blob)
        self.assertFalse(parsed.riff_size_matches_file)


class TestFormatRejection(unittest.TestCase):
    def test_bad_magic(self):
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(b"RF64" + b"\x00" * 20)

    def test_bad_wave_id(self):
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"AVI " + chunk(b"fmt ", fmt_body())))

    def test_float_pcm_rejected(self):
        # wFormatTag=3 (IEEE float), 32 位
        body = chunk(b"fmt ", fmt_body(bits=32, tag=3)) + chunk(
            b"data", b"\x00" * 16
        )
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"WAVE" + body))

    def test_8bit_rejected(self):
        body = chunk(b"fmt ", fmt_body(bits=8)) + chunk(b"data", b"\x00" * 8)
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"WAVE" + body))

    def test_32bit_int_rejected(self):
        body = chunk(b"fmt ", fmt_body(bits=32)) + chunk(b"data", b"\x00" * 16)
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"WAVE" + body))

    def test_missing_fmt(self):
        body = chunk(b"data", b"\x00" * 8)
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"WAVE" + body))

    def test_missing_data(self):
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"WAVE" + chunk(b"fmt ", fmt_body())))

    def test_duplicate_fmt(self):
        body = (
            chunk(b"fmt ", fmt_body())
            + chunk(b"fmt ", fmt_body())
            + chunk(b"data", b"\x00" * 4)
        )
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"WAVE" + body))

    def test_byte_rate_contradiction(self):
        bad = fmt_body()
        bad = bad[:8] + struct.pack("<I", 12345) + bad[12:]
        body = chunk(b"fmt ", bad) + chunk(b"data", b"\x00" * 4)
        with self.assertRaises(WavFormatError):
            wavio.parse_wav(riff(b"WAVE" + body))

    def test_write_32bit_rejected(self):
        with self.assertRaises(WavFormatError):
            wavio.build_wav_bytes(np.array([1], dtype=np.int32), 48000, 1, 32)


if __name__ == "__main__":
    unittest.main()
