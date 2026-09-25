"""离线信号处理服务：纯函数 + JSON 请求驱动，无网络/无界面。

一个"请求"是如下结构的 dict（通常从 JSON 文件读入）：

    {
      "action": "synthesize" | "inspect_wav" | "convert"
                | "wav_to_raw" | "raw_to_wav",
      ...动作特定字段...
    }

所有路径相对运行时工作目录解析。返回值全部由 JSON 可序列化的 Python 类型
（数字/字符串/列表/字典/布尔）组成，用于 stdout 与 *.json 报告；音频落盘
只产生 .wav / .raw / .npy 数据文件。本模块不做任何播放或界面渲染。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import numpy as np

from . import wavio
from .amplitude import from_float, int_max, int_min, to_float
from .errors import PcmAlignmentError, PcmError
from .signals import synthesize

SUPPORTED_BITS = (16, 24)


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def signal_stats(amplitudes: np.ndarray) -> Dict[str, float]:
    """对 float 振幅（任意形状）计算数值统计，全部转成原生 Python float。"""
    flat = np.asarray(amplitudes, dtype=np.float64).reshape(-1)
    if flat.size == 0:
        raise PcmError("无法对空信号计算统计量")
    peak_idx = int(np.argmax(np.abs(flat)))
    return {
        "n_samples": int(flat.size),
        "min": float(flat.min()),
        "max": float(flat.max()),
        "mean": float(flat.mean()),
        "rms": float(np.sqrt(np.mean(flat * flat))),
        "peak_abs": float(np.abs(flat).max()),
        "peak_index": peak_idx,
        "peak_value": float(flat[peak_idx]),
    }


def _stats_from_int(samples: np.ndarray, bits: int) -> Dict[str, float]:
    return signal_stats(to_float(samples, bits))


# --------------------------------------------------------------------------- #
# 公共辅助
# --------------------------------------------------------------------------- #


def load_samples_npy(path: str) -> np.ndarray:
    """读取 .npy 振幅文件（float 或 int 均可，统一转 float64 一维/二维）。"""
    arr = np.load(path, allow_pickle=False)
    return np.asarray(arr, dtype=np.float64)


def _resolve(path: str) -> str:
    return os.fspath(path)


def _decode_raw_pcm(raw: bytes, channels: int, bits: int) -> np.ndarray:
    """原始交错 PCM 字节 -> (n_frames, channels) int32（长度须精确对齐）。"""
    block_align = channels * (bits // 8)
    if len(raw) == 0:
        raise PcmError("raw PCM 文件为空")
    if len(raw) % block_align != 0:
        raise PcmAlignmentError(
            f"raw PCM 长度 {len(raw)} 字节不是 block align {block_align} "
            f"({channels} 声道 x {bits} 位) 的整数倍"
        )
    return wavio._decode_pcm(raw, channels, bits)


def _samples_from_request(req: Dict[str, Any]) -> np.ndarray:
    """从 input_wav 或 input_raw 读取整数样本，返回 (WavData-like, samples)。"""
    if "input_wav" in req:
        data = wavio.read_wav(_resolve(req["input_wav"]))
        return data
    if "input_raw" in req:
        channels = int(req.get("channels", 1))
        bits = int(req.get("bits", 16))
        if bits not in SUPPORTED_BITS:
            raise PcmError(f"仅支持 {SUPPORTED_BITS} 位，得到 {bits}")
        with open(_resolve(req["input_raw"]), "rb") as f:
            raw = f.read()
        samples = _decode_raw_pcm(raw, channels, bits)
        rate = int(req.get("sample_rate", 48000))
        data = wavio.WavData(
            samples=samples,
            sample_rate=rate,
            channels=channels,
            bits=bits,
            riff_size=0,
            actual_payload=0,
            fmt_size=16,
        )
        return data
    raise PcmError("请求缺少输入：需要 input_wav 或 input_raw")


# --------------------------------------------------------------------------- #
# 各动作
# --------------------------------------------------------------------------- #


def _action_synthesize(req: Dict[str, Any]) -> Dict[str, Any]:
    wave_type = str(req.get("wave_type", "sine"))
    sample_rate = int(req.get("sample_rate", 48000))
    duration = float(req.get("duration", 1.0))
    bits = int(req.get("bits", 16))
    if bits not in SUPPORTED_BITS:
        raise PcmError(f"仅支持 {SUPPORTED_BITS} 位，得到 {bits}")
    channels = int(req.get("channels", 1))
    if channels < 1:
        raise PcmError(f"声道数必须 >= 1，得到 {channels}")

    amp = float(req.get("amplitude", 0.8))
    signal = synthesize(
        wave_type=wave_type,
        sample_rate=sample_rate,
        duration=duration,
        frequency=float(req.get("frequency", 440.0)),
        amplitude=amp,
        phase=float(req.get("phase", 0.0)),
        frequency_end=(
            float(req["frequency_end"]) if "frequency_end" in req else None
        ),
        seed=int(req.get("seed", 0)),
    )

    # 复制到多声道（交错排列由 (n, channels) 数组自然表达）。
    if channels > 1:
        signal = np.repeat(signal[:, None], channels, axis=1)

    samples = from_float(signal, bits)
    # 裁剪计数：舍入后会落在端点码且原始缩放值超出"端点码 +-0.5 舍入带"
    # 的样本数，即 from_float 真正改动了舍入结果的数量。
    scaled = signal * np.float64(1 << (bits - 1))
    lo_band = int_min(bits) - 0.5
    hi_band = int_max(bits) + 0.5
    clipped = int(np.count_nonzero((scaled < lo_band) | (scaled > hi_band)))
    result: Dict[str, Any] = {
        "action": "synthesize",
        "wave_type": wave_type,
        "sample_rate": sample_rate,
        "channels": channels,
        "bits": bits,
        "n_frames": int(samples.shape[0]),
        "requested_amplitude": amp,
        "clipped_samples": clipped,
        "stats_float": signal_stats(signal),
        "stats_encoded": _stats_from_int(samples, bits),
        "code_range": {"min": int_min(bits), "max": int_max(bits)},
    }

    if "output_wav" in req:
        n = wavio.write_wav(
            _resolve(req["output_wav"]), samples, sample_rate, channels, bits
        )
        result["output_wav"] = {"path": req["output_wav"], "bytes": n}
    if "output_npy" in req:
        np.save(_resolve(req["output_npy"]), signal)
        result["output_npy"] = {"path": req["output_npy"]}
    return result


def _action_inspect_wav(req: Dict[str, Any]) -> Dict[str, Any]:
    data = wavio.read_wav(_resolve(req["input_wav"]))
    result: Dict[str, Any] = {
        "action": "inspect_wav",
        "input_wav": req["input_wav"],
        "container": data.to_report(),
        "stats_float": _stats_from_int(data.samples, data.bits),
    }
    if "output_npy" in req:
        amps = to_float(data.samples, data.bits)
        np.save(_resolve(req["output_npy"]), amps)
        result["output_npy"] = {"path": req["output_npy"]}
    if "output_raw" in req:
        raw = _pack_raw(data.samples, data.bits)
        with open(_resolve(req["output_raw"]), "wb") as f:
            f.write(raw)
        result["output_raw"] = {"path": req["output_raw"], "bytes": len(raw)}
    return result


def _pack_raw(samples: np.ndarray, bits: int) -> bytes:
    """int32 样本数组 -> 小端交错原始 PCM 字节。"""
    arr = np.asarray(samples)
    if bits == 16:
        return arr.astype("<i2", copy=False).tobytes()
    return wavio._encode_int24(arr)


def _action_wav_to_raw(req: Dict[str, Any]) -> Dict[str, Any]:
    if "output_raw" not in req:
        raise PcmError("wav_to_raw 需要 output_raw")
    data = wavio.read_wav(_resolve(req["input_wav"]))
    raw = _pack_raw(data.samples, data.bits)
    with open(_resolve(req["output_raw"]), "wb") as f:
        f.write(raw)
    return {
        "action": "wav_to_raw",
        "input_wav": req["input_wav"],
        "output_raw": {"path": req["output_raw"], "bytes": len(raw)},
        "sample_rate": data.sample_rate,
        "channels": data.channels,
        "bits": data.bits,
        "n_frames": data.n_frames,
    }


def _action_raw_to_wav(req: Dict[str, Any]) -> Dict[str, Any]:
    if "output_wav" not in req:
        raise PcmError("raw_to_wav 需要 output_wav")
    channels = int(req.get("channels", 1))
    bits = int(req.get("bits", 16))
    sample_rate = int(req.get("sample_rate", 48000))
    if bits not in SUPPORTED_BITS:
        raise PcmError(f"仅支持 {SUPPORTED_BITS} 位，得到 {bits}")
    with open(_resolve(req["input_raw"]), "rb") as f:
        raw = f.read()
    samples = _decode_raw_pcm(raw, channels, bits)
    n = wavio.write_wav(
        _resolve(req["output_wav"]), samples, sample_rate, channels, bits
    )
    return {
        "action": "raw_to_wav",
        "input_raw": req["input_raw"],
        "output_wav": {"path": req["output_wav"], "bytes": n},
        "sample_rate": sample_rate,
        "channels": channels,
        "bits": bits,
        "n_frames": int(samples.shape[0]),
        "stats_float": _stats_from_int(samples, bits),
    }


def _action_convert(req: Dict[str, Any]) -> Dict[str, Any]:
    """读入 WAV（或 raw），按请求的目标位深/容器重新写出，并给出统计。"""
    if "output_wav" not in req:
        raise PcmError("convert 需要 output_wav")
    data = _samples_from_request(req)
    out_bits = int(req.get("output_bits", data.bits))
    if out_bits not in SUPPORTED_BITS:
        raise PcmError(f"仅支持 {SUPPORTED_BITS} 位输出，得到 {out_bits}")
    out_rate = int(req.get("output_sample_rate", data.sample_rate))
    if out_rate != data.sample_rate:
        # 本服务不做重采样：采样率转换不在"容器校验"范围内，明确拒绝。
        raise PcmError(
            f"不支持重采样（{data.sample_rate} -> {out_rate}）；"
            "本服务仅做容器/位深转换"
        )

    amps = to_float(data.samples, data.bits)
    out_samples = from_float(amps, out_bits)
    pre_min, pre_max = int(data.samples.min()), int(data.samples.max())
    post_min, post_max = int(out_samples.min()), int(out_samples.max())
    # 位深降级时统计落在目标码域外的帧数（这些帧经 from_float 被裁剪）。
    out_of_domain = int(
        np.count_nonzero(
            (data.samples < int_min(out_bits))
            | (data.samples > int_max(out_bits))
        )
    )

    n = wavio.write_wav(
        _resolve(req["output_wav"]),
        out_samples,
        out_rate,
        data.channels,
        out_bits,
    )
    return {
        "action": "convert",
        "input_bits": data.bits,
        "output_bits": out_bits,
        "sample_rate": out_rate,
        "channels": data.channels,
        "n_frames": data.n_frames,
        "output_wav": {"path": req["output_wav"], "bytes": n},
        "samples_out_of_target_domain": out_of_domain,
        "stats_input": _stats_from_int(data.samples, data.bits),
        "stats_output": _stats_from_int(out_samples, out_bits),
        "input_code_range": {"min": pre_min, "max": pre_max},
        "output_code_range": {"min": post_min, "max": post_max},
    }


_ACTIONS = {
    "synthesize": _action_synthesize,
    "inspect_wav": _action_inspect_wav,
    "wav_to_raw": _action_wav_to_raw,
    "raw_to_wav": _action_raw_to_wav,
    "convert": _action_convert,
}


def run_request(req: Dict[str, Any]) -> Dict[str, Any]:
    """执行单个请求 dict，返回 JSON 可序列化结果 dict。

    任何 PcmError 都不在此处吞掉——由 CLI 层转换为退出码 2 的错误响应。
    """
    if not isinstance(req, dict):
        raise PcmError(f"请求必须是 JSON 对象，得到 {type(req).__name__}")
    action = req.get("action")
    if action not in _ACTIONS:
        raise PcmError(
            f"未知 action {action!r}，可选：{', '.join(sorted(_ACTIONS))}"
        )
    return _ACTIONS[action](req)


def run_request_file(path: str) -> Dict[str, Any]:
    """从 JSON 文件读入单个请求并执行。"""
    with open(_resolve(path), "rb") as f:
        req = json.load(f)
    return run_request(req)
