"""离线服务层:把 JSON 请求映射为数值结果与文件,不做播放器/界面。

请求动作:
- validate : 校验本地 WAV / raw PCM 文件的容器结构,返回诊断信息。
- synthesize: 合成信号 -> 量化 -> 写出 WAV(可同时写 raw PCM)。
- roundtrip : 合成(或读本地)信号,经 WAV 写出/读回,验证字节与振幅往返。

所有动作只返回 Python 基本类型组成的结果(dict/list/数字/字符串),
可直接 json.dumps。
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np

from . import pcm, synth, wavio


class BadRequestError(ValueError):
    """请求参数非法。"""


def _sample_stats(samples: np.ndarray, bits: int) -> dict[str, Any]:
    flat = np.asarray(samples).reshape(-1)
    lo, hi = pcm.int_range(bits)
    if flat.size == 0:
        return {"count": 0}
    amp = pcm.dequantize(flat.astype(pcm.int_dtype(bits)), bits)
    return {
        "count": int(flat.size),
        "int_min": int(flat.min()),
        "int_max": int(flat.max()),
        "amplitude_min": float(amp.min()),
        "amplitude_max": float(amp.max()),
        "zero_crossings": int(np.count_nonzero(np.diff(np.signbit(flat)))),
        "extreme_negative_count": int(np.count_nonzero(flat == lo)),
        "extreme_positive_count": int(np.count_nonzero(flat == hi)),
    }


def _chunks_summary(result: wavio.WavReadResult) -> list[dict[str, Any]]:
    return [
        {
            "id": c.id,
            "offset": c.offset,
            "declared_size": c.size,
            "padded_size": c.padded_size,
            "handled": c.handled,
            "truncated": c.truncated,
        }
        for c in result.chunks
    ]


def _issues(result_or_issues: wavio.WavReadResult | list[wavio.WavIssue]) -> list[dict[str, Any]]:
    issues = (
        result_or_issues.issues
        if isinstance(result_or_issues, wavio.WavReadResult)
        else result_or_issues
    )
    return [
        {"code": i.code, "severity": i.severity, "message": i.message}
        for i in issues
    ]


def _validate_wav(req: dict[str, Any]) -> dict[str, Any]:
    path = req.get("path")
    if not isinstance(path, str) or not path:
        raise BadRequestError("validate(path='*.wav') requires a path string")
    strict = bool(req.get("strict", False))
    result = wavio.read_wav(path, strict=strict)
    return {
        "status": "ok",
        "container": "RIFF/WAVE",
        "path": path,
        "file_size": os.path.getsize(path),
        "format_tag": result.format_tag,
        "sample_rate": result.sample_rate,
        "channels": result.channels,
        "bits_per_sample": result.bits_per_sample,
        "frames": result.frames,
        "data_size": result.data_size,
        "data_offset": result.data_offset,
        "chunks": _chunks_summary(result),
        "issues": _issues(result),
        "sample_stats": _sample_stats(result.samples, result.bits_per_sample),
    }


def _validate_raw(req: dict[str, Any]) -> dict[str, Any]:
    """校验无容器 raw PCM:按请求提供的采样率/声道/位深解码。"""
    path = req.get("path")
    bits = int(req.get("bits", 16))
    channels = int(req.get("channels", 1))
    if not isinstance(path, str) or not path:
        raise BadRequestError("validate(raw) requires a path string")
    if bits not in pcm.SUPPORTED_BIT_DEPTHS:
        raise BadRequestError(
            f"bits must be one of {pcm.SUPPORTED_BIT_DEPTHS}, got {bits}"
        )
    if channels < 1:
        raise BadRequestError(f"channels must be >= 1, got {channels}")

    with open(path, "rb") as fh:
        raw = fh.read()
    issues: list[wavio.WavIssue] = []
    frame_bytes = channels * (bits // 8)
    remainder = len(raw) % frame_bytes
    if remainder:
        issues.append(
            wavio.WavIssue(
                "PARTIAL_FRAME",
                f"raw size {len(raw)} not a multiple of frame size "
                f"{frame_bytes}; last {remainder} byte(s) ignored",
            )
        )
    usable = raw[: len(raw) - remainder] if remainder else raw
    samples = pcm.decode_pcm(usable, bits)
    frames = samples.size // channels
    reshaped = samples.reshape(frames, channels) if channels > 1 else samples
    return {
        "status": "ok",
        "container": "raw-pcm",
        "path": path,
        "file_size": len(raw),
        "sample_rate": int(req.get("sample_rate", 0)) or None,
        "channels": channels,
        "bits_per_sample": bits,
        "frames": frames,
        "issues": _issues(issues),
        "sample_stats": _sample_stats(reshaped, bits),
    }


def _synthesize(req: dict[str, Any]) -> dict[str, Any]:
    kind = str(req.get("kind", "sine"))
    sample_rate = int(req.get("sample_rate", 8000))
    duration = float(req.get("duration", 0.01))
    bits = int(req.get("bits", 16))
    channels = int(req.get("channels", 1))
    amplitude = float(req.get("amplitude", 0.5))
    out = req.get("out")
    raw_out = req.get("raw_out")

    if bits not in pcm.SUPPORTED_BIT_DEPTHS:
        raise BadRequestError(
            f"bits must be one of {pcm.SUPPORTED_BIT_DEPTHS}, got {bits}"
        )
    if channels < 1 or sample_rate <= 0 or duration <= 0:
        raise BadRequestError("channels>=1, sample_rate>0 and duration>0 are required")

    mono = synth.make(kind, sample_rate, duration, bits, amplitude)
    if channels > 1:
        samples_f = np.repeat(mono[:, None], channels, axis=1)
    else:
        samples_f = mono
    samples_i = pcm.quantize(samples_f, bits)

    files: list[str] = []
    if raw_out:
        flat = samples_i.reshape(-1)
        with open(raw_out, "wb") as fh:
            fh.write(pcm.encode_pcm(flat, bits))
        files.append(str(raw_out))
    if out:
        wavio.write_wav(str(out), samples_i, sample_rate, channels, bits)
        files.append(str(out))

    return {
        "status": "ok",
        "action": "synthesize",
        "kind": kind,
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_sample": bits,
        "frames": int(mono.size),
        "files": files,
        "sample_stats": _sample_stats(samples_i, bits),
    }


def _build_roundtrip_source(req: dict[str, Any], bits: int, channels: int, sample_rate: int):
    """返回 (浮点源信号或 None, 输入 WAV 路径或 None)。"""
    if "path" in req and req["path"]:
        return None, str(req["path"])
    kind = str(req.get("kind", "fullscale"))
    duration = float(req.get("duration", 0.01))
    amplitude = float(req.get("amplitude", 0.5))
    mono = synth.make(kind, sample_rate, duration, bits, amplitude)
    if channels > 1:
        return np.repeat(mono[:, None], channels, axis=1), None
    return mono, None


def _roundtrip(req: dict[str, Any]) -> dict[str, Any]:
    sample_rate = int(req.get("sample_rate", 8000))
    bits = int(req.get("bits", 16))
    channels = int(req.get("channels", 1))
    out = req.get("out")
    if bits not in pcm.SUPPORTED_BIT_DEPTHS:
        raise BadRequestError(
            f"bits must be one of {pcm.SUPPORTED_BIT_DEPTHS}, got {bits}"
        )

    source_f, input_path = _build_roundtrip_source(req, bits, channels, sample_rate)
    if input_path is not None:
        readback = wavio.read_wav(input_path)
        sample_rate = readback.sample_rate
        channels = readback.channels
        bits = readback.bits_per_sample
        samples_i = readback.samples
        origin = f"wav:{input_path}"
        source_label = "decoded integers"
    else:
        samples_i = pcm.quantize(source_f, bits)
        origin = "synthetic"
        source_label = "pre-quantization floats"

    blob = wavio.encode_wav(samples_i, sample_rate, channels, bits)
    if out:
        with open(out, "wb") as fh:
            fh.write(blob)

    reparsed = wavio.read_wav(blob)

    # 1) 字节级往返:原始 PCM 负载 -> 写回 -> 读回,字节完全一致。
    flat_in = np.asarray(samples_i).reshape(-1)
    flat_out = reparsed.samples.reshape(-1)
    payload_bytes = pcm.encode_pcm(flat_in, bits)
    reparsed_bytes = blob[reparsed.data_offset : reparsed.data_offset + reparsed.data_size]
    bytes_identical = payload_bytes == reparsed_bytes
    # 整文件:读现有 WAV 再重写(无未知 chunk 时)应字节一致。
    whole_file_identical = None
    if input_path is not None:
        with open(input_path, "rb") as fh:
            original_bytes = fh.read()
        whole_file_identical = original_bytes == blob

    # 2) 振幅往返:整数域反量化后再读回应完全一致;
    #    对合成浮点源,量化引入的误差不超过 0.5 LSB。
    amp_roundtrip = pcm.dequantize(flat_out, bits)
    int_amp_identical = bool(
        np.array_equal(amp_roundtrip, pcm.dequantize(flat_in.astype(pcm.int_dtype(bits)), bits))
    )
    max_quant_error = None
    if source_f is not None:
        max_quant_error = float(
            np.abs(amp_roundtrip - np.asarray(source_f, np.float64).reshape(-1)).max()
        )

    return {
        "status": "ok",
        "action": "roundtrip",
        "origin": origin,
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_sample": bits,
        "frames": reparsed.frames,
        "wav_bytes": len(blob),
        "file": str(out) if out else None,
        "checks": {
            "payload_bytes_identical": bool(bytes_identical),
            "integer_samples_identical": bool(np.array_equal(flat_in, flat_out)),
            "amplitude_roundtrip_identical": int_amp_identical,
            "whole_file_rebuild_identical": whole_file_identical,
        },
        "quantization": {
            "source": source_label,
            "max_abs_error_amplitude": max_quant_error,
            "half_lsb_amplitude": 0.5 / float(1 << (bits - 1)),
        },
        "sample_stats": _sample_stats(flat_out, bits),
        "issues": _issues(reparsed),
    }


def handle(request: dict[str, Any]) -> dict[str, Any]:
    """处理单个请求字典。未知动作或参数错误抛 BadRequestError。"""
    if not isinstance(request, dict):
        raise BadRequestError("request must be a JSON object")
    action = request.get("action")
    if action == "validate":
        return _validate_raw(request) if request.get("container") == "raw" else _validate_wav(request)
    if action == "synthesize":
        return _synthesize(request)
    if action == "roundtrip":
        return _roundtrip(request)
    raise BadRequestError(
        f"unknown action {action!r}; expected validate|synthesize|roundtrip"
    )
