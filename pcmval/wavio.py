"""WAV(RIFF/WAVE)PCM 子集的读取、校验与写出。

支持的子集:
- RIFF/WAVE 容器,fmt + data 两个必需 chunk,其余 chunk 一律跳过并记录。
- WAVE_FORMAT_PCM (0x0001) 与 WAVE_FORMAT_EXTENSIBLE (0xFFFE,
  子格式必须为 PCM GUID)。
- 位深仅 16 / 24 bit 整数 PCM,其余格式与位深一律拒绝。

容器规则:
- 每个 chunk 为 8 字节头(id + 小端 u32 长度)+ 负载;负载长度为奇数时
  后跟 1 字节补齐(pad),pad 计入 RIFF 长度但不计入 chunk 长度。
- 读端对未知 chunk 按长度跳过(含 pad);声明长度越过文件末尾视为
  截断(chunk 头截断与负载截断分别报告)。
- RIFF 头声明的总长度与实际文件不一致时记 warning(不拒绝)。
- data 字节数不是 block_align 整数倍时,丢弃末尾残缺帧并记 warning。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from . import pcm

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

# KSDATAFORMAT_SUBTYPE_PCM: 00000001-0000-0010-8000-00aa00389b71 (小端布局)
_PCM_SUBTYPE_GUID = bytes.fromhex("0100000000001000800000aa00389b71")

FMT_CHUNK_MIN_SIZE = 16
EXTENSIBLE_MIN_SIZE = 40  # 16 字节基础字段 + cbSize(22 起)


class WavFormatError(ValueError):
    """WAV 容器结构或格式不支持。"""


@dataclass
class WavIssue:
    """一条非致命问题记录。severity: "warning" | "error"。"""

    code: str
    message: str
    severity: str = "warning"


@dataclass
class WavChunkInfo:
    """chunk 目录条目(含被跳过的未知 chunk)。"""

    id: str
    offset: int          # chunk 头在文件中的偏移
    size: int            # 声明的负载长度(不含 pad)
    padded_size: int     # 负载 + pad
    handled: str         # "fmt" | "data" | "skipped"
    truncated: bool = False


@dataclass
class WavReadResult:
    """read_wav 的返回:样本 + 容器校验报告。"""

    samples: np.ndarray          # int16 或 int32;单声道一维,多声道 (frames, channels)
    sample_rate: int
    channels: int
    bits_per_sample: int
    format_tag: int
    frames: int
    data_size: int               # data chunk 声明的负载长度
    data_offset: int             # data 负载在文件中的偏移
    chunks: list[WavChunkInfo] = field(default_factory=list)
    issues: list[WavIssue] = field(default_factory=list)

    @property
    def floats(self) -> np.ndarray:
        """浮点振幅视图 [-1.0, 1.0)。"""
        return pcm.dequantize(self.samples, self.bits_per_sample)


def _chunk_id(raw: bytes) -> str:
    return raw.decode("latin-1")


def _parse_fmt(payload: bytes, result: WavReadResult) -> None:
    if len(payload) < FMT_CHUNK_MIN_SIZE:
        raise WavFormatError(
            f"fmt chunk too small: {len(payload)} bytes (need >= {FMT_CHUNK_MIN_SIZE})"
        )
    (tag, channels, sample_rate, byte_rate, block_align, bits) = struct.unpack(
        "<HHIIHH", payload[:16]
    )
    result.format_tag = tag
    result.channels = channels
    result.sample_rate = sample_rate
    result.bits_per_sample = bits

    if tag == WAVE_FORMAT_EXTENSIBLE:
        if len(payload) < EXTENSIBLE_MIN_SIZE:
            raise WavFormatError(
                f"WAVE_FORMAT_EXTENSIBLE fmt chunk too small: {len(payload)} bytes"
            )
        valid_bits, _channel_mask = struct.unpack("<HI", payload[18:24])
        sub_tag = struct.unpack("<H", payload[24:26])[0]
        if payload[24:40] != _PCM_SUBTYPE_GUID:
            raise WavFormatError(
                f"unsupported extensible sub-format GUID: {payload[24:40].hex()}"
            )
        if valid_bits != bits:
            result.issues.append(
                WavIssue(
                    "VALID_BITS_MISMATCH",
                    f"extensible valid_bits={valid_bits} differs from "
                    f"container bits_per_sample={bits}; decoding as {bits}-bit",
                )
            )
        result.format_tag = sub_tag  # 归一化为 PCM
    elif tag != WAVE_FORMAT_PCM:
        raise WavFormatError(
            f"unsupported format tag 0x{tag:04x} "
            f"(only PCM 0x0001 / extensible-with-PCM-subtype 0xfffe)"
        )

    if bits not in pcm.SUPPORTED_BIT_DEPTHS:
        raise WavFormatError(
            f"unsupported bit depth {bits}; supported: {pcm.SUPPORTED_BIT_DEPTHS}"
        )
    if channels < 1:
        raise WavFormatError(f"invalid channel count {channels}")

    expected_align = channels * (bits // 8)
    if block_align != expected_align:
        raise WavFormatError(
            f"block_align mismatch: header says {block_align}, "
            f"expected {expected_align} (= {channels}ch * {bits // 8}B)"
        )
    expected_byte_rate = sample_rate * expected_align
    if byte_rate != expected_byte_rate:
        result.issues.append(
            WavIssue(
                "BYTE_RATE_MISMATCH",
                f"byte_rate {byte_rate} != sample_rate*block_align "
                f"{expected_byte_rate}",
            )
        )


def read_wav(source: str | bytes, strict: bool = False) -> WavReadResult:
    """读取并校验 WAV 文件。

    source: 文件路径或字节串。strict=True 时 warning 也按异常抛出。
    结构性错误(非 RIFF/WAVE、缺 fmt/data、格式不支持、chunk 截断等)
    一律抛 WavFormatError;非致命问题记录在 result.issues。
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    else:
        with open(source, "rb") as fh:
            data = fh.read()

    if len(data) < 12:
        raise WavFormatError(
            f"file too small for a RIFF/WAVE header: {len(data)} bytes"
        )
    if data[0:4] != b"RIFF":
        raise WavFormatError(f"not a RIFF file (magic={data[0:4]!r})")
    if data[8:12] != b"WAVE":
        raise WavFormatError(f"not a WAVE file (form type={data[8:12]!r})")

    riff_size = struct.unpack("<I", data[4:8])[0]
    file_end = len(data)
    riff_end = 8 + riff_size
    issues: list[WavIssue] = []
    if riff_end != file_end:
        issues.append(
            WavIssue(
                "RIFF_SIZE_MISMATCH",
                f"RIFF header declares {riff_end} bytes total, "
                f"file has {file_end} bytes",
            )
        )

    result = WavReadResult(
        samples=np.empty(0, dtype=np.int16),
        sample_rate=0,
        channels=0,
        bits_per_sample=0,
        format_tag=0,
        frames=0,
        data_size=0,
        data_offset=0,
        issues=issues,
    )

    fmt_payload: bytes | None = None
    data_payload: bytes | None = None
    extra_data_chunks = 0

    # chunk 遍历以真实文件末尾为界;RIFF 声明长度只用于上面的 warning,
    # 这样截断文件里完整保留的 chunk 仍可被诊断。
    pos = 12
    while pos < file_end:
        if pos + 8 > file_end:
            # 末尾不足一个 chunk 头的残余字节:按尾部垃圾告警,不视为截断
            issues.append(
                WavIssue(
                    "TRAILING_BYTES",
                    f"{file_end - pos} trailing byte(s) after last chunk "
                    f"at offset {pos} ignored",
                )
            )
            break
        cid = _chunk_id(data[pos : pos + 4])
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        payload_start = pos + 8
        payload_end = payload_start + size
        padded = size + (size & 1)

        if payload_end > file_end:
            result.chunks.append(
                WavChunkInfo(cid, pos, size, padded, "skipped", truncated=True)
            )
            raise WavFormatError(
                f"truncated chunk {cid!r} at offset {pos}: declares {size} "
                f"payload bytes, only {file_end - payload_start} remain"
            )

        if cid == "fmt " and fmt_payload is None:
            fmt_payload = data[payload_start:payload_end]
            handled = "fmt"
        elif cid == "data" and data_payload is None:
            data_payload = data[payload_start:payload_end]
            result.data_size = size
            result.data_offset = payload_start
            handled = "data"
        else:
            if cid == "data":
                extra_data_chunks += 1
            handled = "skipped"
        result.chunks.append(WavChunkInfo(cid, pos, size, padded, handled))
        pos = payload_start + padded

    if extra_data_chunks:
        issues.append(
            WavIssue(
                "EXTRA_DATA_CHUNK",
                f"{extra_data_chunks} additional data chunk(s) ignored",
            )
        )

    if fmt_payload is None:
        raise WavFormatError("missing fmt chunk")
    _parse_fmt(fmt_payload, result)

    if data_payload is None:
        raise WavFormatError("missing data chunk")

    block_align = result.channels * (result.bits_per_sample // 8)
    remainder = result.data_size % block_align
    if remainder:
        issues.append(
            WavIssue(
                "PARTIAL_FRAME",
                f"data size {result.data_size} is not a multiple of "
                f"block_align {block_align}; dropped {remainder} trailing byte(s)",
            )
        )
        data_payload = data_payload[: result.data_size - remainder]

    flat = pcm.decode_pcm(data_payload, result.bits_per_sample)
    frames = flat.size // result.channels
    result.frames = frames
    if result.channels == 1:
        result.samples = flat
    else:
        result.samples = flat.reshape(frames, result.channels)

    if strict and any(i.severity == "warning" for i in issues):
        raise WavFormatError(
            "strict mode: " + "; ".join(i.message for i in issues)
        )
    return result


def encode_wav(
    samples: np.ndarray,
    sample_rate: int,
    channels: int,
    bits: int,
    *,
    extra_chunks: list[tuple[bytes, bytes]] | None = None,
) -> bytes:
    """把整数样本编码为完整 WAV 字节串。

    samples: int16(bits=16)或 int32(bits=24);一维(单声道)或
    (frames, channels)。extra_chunks 可注入额外 chunk(测试用),
    每项为 (4 字节 id, 负载);奇数长度负载自动补 pad 并计入 RIFF 长度。
    """
    if bits not in pcm.SUPPORTED_BIT_DEPTHS:
        raise pcm.UnsupportedBitDepthError(
            f"unsupported bit depth {bits}; supported: {pcm.SUPPORTED_BIT_DEPTHS}"
        )
    if channels < 1:
        raise ValueError(f"invalid channel count {channels}")
    if sample_rate <= 0:
        raise ValueError(f"invalid sample rate {sample_rate}")

    arr = np.asarray(samples)
    if arr.ndim == 2:
        if arr.shape[1] != channels:
            raise ValueError(
                f"samples have {arr.shape[1]} channels, expected {channels}"
            )
        flat = arr.reshape(-1)
    elif arr.ndim == 1:
        if channels != 1 and arr.size % channels != 0:
            raise ValueError(
                f"{arr.size} samples cannot be reshaped to {channels} channels"
            )
        flat = arr
    else:
        raise ValueError(f"samples must be 1-D or 2-D, got {arr.ndim}-D")

    block_align = channels * (bits // 8)
    byte_rate = sample_rate * block_align
    fmt_payload = struct.pack(
        "<HHIIHH", WAVE_FORMAT_PCM, channels, sample_rate,
        byte_rate, block_align, bits,
    )
    data_payload = pcm.encode_pcm(flat, bits)

    chunks: list[tuple[bytes, bytes]] = [(b"fmt ", fmt_payload)]
    if extra_chunks:
        chunks.extend(extra_chunks)
    chunks.append((b"data", data_payload))

    body = bytearray()
    for cid, payload in chunks:
        if len(cid) != 4:
            raise ValueError(f"chunk id must be 4 bytes, got {cid!r}")
        body += cid
        body += struct.pack("<I", len(payload))
        body += payload
        if len(payload) & 1:
            body += b"\x00"  # 奇数字节补齐,pad 计入 RIFF 长度

    riff_size = 4 + len(body)  # "WAVE" + 全部 chunk(含 pad)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + bytes(body)


def write_wav(
    path: str,
    samples: np.ndarray,
    sample_rate: int,
    channels: int = 1,
    bits: int = 16,
) -> dict:
    """写出 WAV 文件。浮点输入按 [-1.0, 1.0) 量化,整数输入原样写出。"""
    arr = np.asarray(samples)
    if np.issubdtype(arr.dtype, np.floating):
        arr = pcm.quantize(arr, bits)
    blob = encode_wav(arr, sample_rate, channels, bits)
    with open(path, "wb") as fh:
        fh.write(blob)
    return {
        "path": str(path),
        "bytes": len(blob),
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_sample": bits,
        "frames": arr.size // channels,
    }
