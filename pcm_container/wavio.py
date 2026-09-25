"""WAV (RIFF/WAVE) PCM 子集读取与写入。

支持的子集（其余一律拒绝，见 WavFormatError）：
- RIFF 容器、WAVE 格式标识；
- PCM（wFormatTag == 1），16 位或 24 位有符号整数，单声道或多声道；
- 恰好一个 fmt chunk、恰好一个 data chunk；
- 未知 chunk 一律跳过（按 chunk 头中的 size 字段寻址，奇数 size 跳过 pad 字节）。

读取的宽容策略（用于"容器校验"）：
- chunk 头（8 字节）读不全、chunk 体读不全 -> WavTruncatedError；
- chunk 位于文件内部但缺少奇数 pad 字节 -> WavTruncatedError；
  若 pad 恰好缺失在文件尾（后面没有任何内容），视为合法宽容接受；
- RIFF 头声明的尺寸与文件实际长度不一致不报错，只在报告里标记
  riff_size_matches_file；
- data 尺寸不是 block align 的整数倍 -> PcmAlignmentError（采样无法对齐）；
- fmt 各字段互相矛盾（block align、字节率与位深/通道/采样率不符）
  -> WavFormatError。

样本以 int32 numpy 数组返回，形状为 (n_frames, channels)。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .amplitude import SUPPORTED_BITS
from .errors import PcmAlignmentError, WavFormatError, WavTruncatedError

_RIFF_MAGIC = b"RIFF"
_WAVE_MAGIC = b"WAVE"
_FMT_ID = b"fmt "
_DATA_ID = b"data"
_PCM_TAG = 0x0001


@dataclass
class WavData:
    """一次成功读取得到的 WAV PCM 数据与容器元信息。"""

    samples: np.ndarray  # shape (n_frames, channels), dtype int32
    sample_rate: int
    channels: int
    bits: int
    riff_size: int  # RIFF 头中声明的尺寸
    actual_payload: int  # 文件实际字节数 - 8
    fmt_size: int
    extra_chunks: List[Tuple[str, bytes]] = field(default_factory=list)
    data_size_declared: int = 0

    @property
    def n_frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def riff_size_matches_file(self) -> bool:
        return self.riff_size == self.actual_payload

    def to_report(self) -> Dict[str, object]:
        """供 JSON 报告使用的纯 Python 类型摘要。"""
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bits": self.bits,
            "n_frames": self.n_frames,
            "data_size_declared": self.data_size_declared,
            "fmt_size": self.fmt_size,
            "riff_size_declared": self.riff_size,
            "file_payload_bytes": self.actual_payload,
            "riff_size_matches_file": self.riff_size_matches_file,
            "extra_chunks": [
                {"id": cid, "size": len(body)} for cid, body in self.extra_chunks
            ],
        }


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def _read_exact(buf: bytes, pos: int, n: int, what: str) -> bytes:
    """从 buf[pos:] 精确读取 n 字节，不足即视为截断。"""
    if pos + n > len(buf):
        raise WavTruncatedError(
            f"{what} 需要 {n} 字节，偏移 {pos} 处仅剩 {len(buf) - pos} 字节"
        )
    return buf[pos : pos + n]


def _decode_pcm(
    raw: bytes, channels: int, bits: int
) -> np.ndarray:
    """把交错排列的小端 PCM 字节解码为 (n_frames, channels) int32。

    调用方需先保证 len(raw) % block_align == 0。
    """
    if bits == 16:
        flat = np.frombuffer(raw, dtype="<i2").astype(np.int32)
    elif bits == 24:
        flat = _decode_int24(raw)
    else:  # 理论不可达：位深在 fmt 解析处已被拦截
        raise WavFormatError(f"不支持的位深: {bits} 位")
    return flat.reshape(-1, channels)


def _decode_int24(raw: bytes) -> np.ndarray:
    """三字节小端有符号整数 -> int32 一维数组。"""
    b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    value = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
    # 24 位符号扩展到 32 位：bit23 为 1 时减去 2^24。
    value = np.where(value >= 1 << 23, value - (1 << 24), value)
    return value.astype(np.int32)


def parse_wav(buf: bytes) -> WavData:
    """解析 WAV 字节串，返回 WavData；不合法子集抛出 PcmError 子类。"""
    if len(buf) < 12:
        raise WavTruncatedError(f"文件不足 12 字节，无法构成 RIFF/WAVE 头（实际 {len(buf)} 字节）")
    if buf[0:4] != _RIFF_MAGIC:
        raise WavFormatError(f"缺少 RIFF 标识，前 4 字节为 {buf[0:4]!r}")
    (riff_size,) = struct.unpack_from("<I", buf, 4)
    if buf[8:12] != _WAVE_MAGIC:
        raise WavFormatError(f"缺少 WAVE 标识，得到 {buf[8:12]!r}")

    fmt_body: Optional[bytes] = None
    data_body: Optional[bytes] = None
    data_size_declared = 0
    extra_chunks: List[Tuple[str, bytes]] = []

    pos = 12
    total = len(buf)
    # chunk 解析终点：
    # - RIFF 声明尺寸落在文件内时，以声明边界为准（尾部杂散字节不参与解析，
    #   只在报告 riff_size_matches_file 中标记）；
    # - 声明尺寸超出文件时，以文件尾为准（chunk 越界由截断检查拦截）。
    riff_end = min(total, 8 + riff_size)
    while pos < riff_end:
        # chunk 头：4 字节 id + 4 字节小端尺寸；不足 8 字节即截断
        # （1 字节以内的残留按 chunk pad 宽容处理，直接结束）。
        remaining = riff_end - pos
        if remaining <= 1:
            break
        header = _read_exact(buf, pos, 8, "chunk 头")
        chunk_id = bytes(header[0:4])
        (chunk_size,) = struct.unpack_from("<I", header, 4)
        body_start = pos + 8
        body_end = body_start + chunk_size
        # body 既不能越过文件，也不能越过 RIFF 声明边界。
        if body_end > total or body_end > riff_end:
            raise WavTruncatedError(
                f"chunk {chunk_id!r} 声明 {chunk_size} 字节，"
                f"偏移 {body_start} 处可用字节不足"
            )
        body = buf[body_start:body_end]

        cid_text = chunk_id.decode("latin-1")
        if chunk_id == _FMT_ID:
            if fmt_body is not None:
                raise WavFormatError("出现第二个 fmt chunk")
            fmt_body = body
        elif chunk_id == _DATA_ID:
            if data_body is not None:
                raise WavFormatError("出现第二个 data chunk")
            data_body = body
            data_size_declared = chunk_size
        else:
            extra_chunks.append((cid_text, body))

        # 奇数 chunk 体后必须有一个 pad 字节，使下一个 chunk 字对齐。
        pos = body_end
        if chunk_size & 1:
            if pos < riff_end:
                # 容器内部：pad 必须真实存在（占一个字节，内容任意）。
                pos += 1
            else:
                # pad 恰好缺失在 RIFF 边界/文件尾：宽容接受（真实文件常见）。
                break

    if fmt_body is None:
        raise WavFormatError("缺少 fmt chunk")
    if data_body is None:
        raise WavFormatError("缺少 data chunk")

    sample_rate, channels, bits = _parse_fmt(fmt_body)
    block_align = channels * (bits // 8)
    if len(data_body) % block_align != 0:
        raise PcmAlignmentError(
            f"data 长度 {len(data_body)} 字节不是 block align {block_align} "
            f"({channels} 声道 x {bits} 位) 的整数倍，采样无法对齐"
        )

    samples = _decode_pcm(data_body, channels, bits)
    return WavData(
        samples=samples,
        sample_rate=sample_rate,
        channels=channels,
        bits=bits,
        riff_size=riff_size,
        actual_payload=total - 8,
        fmt_size=len(fmt_body),
        extra_chunks=extra_chunks,
        data_size_declared=data_size_declared,
    )


def _parse_fmt(body: bytes) -> Tuple[int, int, int]:
    """解析 fmt chunk 体并做子集校验，返回 (sample_rate, channels, bits)。"""
    if len(body) < 16:
        raise WavFormatError(
            f"fmt chunk 过短（{len(body)} 字节，PCM 至少需要 16 字节）"
        )
    format_tag, channels, sample_rate, byte_rate, block_align, bits = (
        struct.unpack_from("<HHIIHH", body, 0)
    )
    if format_tag != _PCM_TAG:
        raise WavFormatError(
            f"仅支持无压缩整数 PCM（wFormatTag=1），文件为 wFormatTag={format_tag}"
            f"{'（可能是 float/ADPCM/其它压缩格式）' if format_tag != 1 else ''}"
        )
    if channels < 1:
        raise WavFormatError(f"声道数必须 >= 1，得到 {channels}")
    if sample_rate == 0:
        raise WavFormatError("采样率为 0")
    if bits not in SUPPORTED_BITS:
        raise WavFormatError(
            f"仅支持 {SUPPORTED_BITS} 位整数 PCM，文件为 {bits} 位"
        )
    expected_block = channels * (bits // 8)
    if block_align != expected_block:
        raise WavFormatError(
            f"fmt block align={block_align} 与 {channels} 声道/{bits} 位"
            f"（应为 {expected_block}）矛盾"
        )
    expected_byte_rate = sample_rate * expected_block
    if byte_rate != expected_byte_rate:
        raise WavFormatError(
            f"fmt 字节率={byte_rate} 与采样率 {sample_rate} x block align "
            f"{expected_block}（应为 {expected_byte_rate}）矛盾"
        )
    return sample_rate, channels, bits


def read_wav(path: str) -> WavData:
    """从磁盘读取 WAV 文件（详见 parse_wav 的校验规则）。"""
    with open(path, "rb") as f:
        return parse_wav(f.read())


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def _encode_int24(samples: np.ndarray) -> bytes:
    """int32（已裁剪到 24 位域）-> 三字节小端字节串。"""
    flat = np.asarray(samples, dtype="<i4").reshape(-1)
    unsigned = flat.astype(np.uint32) & 0x00FFFFFF
    b = np.empty((flat.size, 3), dtype=np.uint8)
    b[:, 0] = unsigned & 0xFF
    b[:, 1] = (unsigned >> 8) & 0xFF
    b[:, 2] = (unsigned >> 16) & 0xFF
    return b.tobytes()


def _chunk(chunk_id: bytes, body: bytes) -> bytes:
    """组装 chunk；body 为奇数长度时追加一个 0x00 pad 字节。"""
    out = chunk_id + struct.pack("<I", len(body)) + body
    if len(body) & 1:
        out += b"\x00"
    return out


def build_wav_bytes(
    samples: np.ndarray,
    sample_rate: int,
    channels: int,
    bits: int,
    extra_chunks: Optional[List[Tuple[str, bytes]]] = None,
) -> bytes:
    """把整数样本序列化为符合规范的 WAV 字节串。

    samples 接受一维（视为单声道，需 channels==1）或 (n_frames, channels)。
    样本必须已在目标位深的码域内，越界抛 ValueError（不做静默裁剪，
    裁剪属于 amplitude.from_float 的显式职责）。
    RIFF 尺寸始终与实际输出字节数严格一致；奇数 chunk 自动补 pad。
    """
    if bits not in SUPPORTED_BITS:
        raise WavFormatError(f"仅支持 {SUPPORTED_BITS} 位 PCM 写出，得到 {bits} 位")
    if channels < 1:
        raise ValueError(f"声道数必须 >= 1，得到 {channels}")
    if sample_rate < 1:
        raise ValueError(f"采样率必须 >= 1，得到 {sample_rate}")

    arr = np.asarray(samples)
    if arr.ndim == 1:
        if channels != 1:
            raise ValueError(
                f"一维样本只能按单声道写出，但 channels={channels}"
            )
        arr = arr.reshape(-1, 1)
    if arr.ndim != 2 or arr.shape[1] != channels:
        raise ValueError(
            f"样本形状 {arr.shape} 与 channels={channels} 不匹配"
        )
    if arr.shape[0] == 0:
        raise ValueError("至少需要一个采样帧")

    from .amplitude import int_max as _imax
    from .amplitude import int_min as _imin

    lo, hi = _imin(bits), _imax(bits)
    mn, mx = int(arr.min()), int(arr.max())
    if mn < lo or mx > hi:
        raise ValueError(
            f"样本超出 {bits} 位码域 [{lo}, {hi}]（实际最小 {mn}，最大 {mx}）"
        )

    ints = arr.astype(np.int32, copy=False)
    if bits == 16:
        data_body = ints.astype("<i2", copy=False).tobytes()
    else:
        data_body = _encode_int24(ints)

    block_align = channels * (bits // 8)
    byte_rate = sample_rate * block_align
    fmt_body = struct.pack(
        "<HHIIHH",
        _PCM_TAG,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
    )

    chunks = _chunk(_FMT_ID, fmt_body)
    for cid, body in extra_chunks or []:
        cid_bytes = cid.encode("latin-1") if isinstance(cid, str) else bytes(cid)
        if len(cid_bytes) != 4:
            raise ValueError(f"自定义 chunk id 必须恰为 4 字节: {cid!r}")
        if cid_bytes in (_FMT_ID, _DATA_ID, _RIFF_MAGIC):
            raise ValueError(f"自定义 chunk id {cid!r} 与保留 chunk 冲突")
        chunks += _chunk(cid_bytes, bytes(body))
    chunks += _chunk(_DATA_ID, data_body)

    riff_size = len(chunks) + 4  # 'WAVE' 4 字节计入 RIFF 尺寸
    return _RIFF_MAGIC + struct.pack("<I", riff_size) + _WAVE_MAGIC + chunks


def write_wav(
    path: str,
    samples: np.ndarray,
    sample_rate: int,
    channels: int,
    bits: int,
    extra_chunks: Optional[List[Tuple[str, bytes]]] = None,
) -> int:
    """写出 WAV 文件，返回写入字节数。"""
    data = build_wav_bytes(samples, sample_rate, channels, bits, extra_chunks)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)
