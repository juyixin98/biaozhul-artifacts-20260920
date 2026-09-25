"""离线分析流水线：解析请求 -> 取两通道 -> 分块相关 -> 汇总 -> 落盘 JSON/NPZ。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import EstimatorConfig
from .correlator import normalized_cross_correlation
from .signals import (
    SyntheticSpec,
    make_delayed_pair,
    read_raw_pcm,
    read_wav,
)
from .windows import estimate_windows


def _load_signals(
    source: dict, base_dir: Path
) -> tuple[np.ndarray, np.ndarray, int | None, dict]:
    """根据 ``source`` 段构造等长 (ref, chan)；返回附带元数据。"""
    if not isinstance(source, dict):
        raise ValueError("source 必须是对象")
    mode = source.get("mode", "synthetic")

    if mode == "synthetic":
        spec = SyntheticSpec.from_dict(source)
        ref, chan = make_delayed_pair(spec)
        meta = {
            "mode": "synthetic",
            "kind": spec.kind,
            "sample_rate": spec.sample_rate,
            "delay_samples_truth": spec.delay_samples,
            "noise_db": spec.noise_db,
            "gain": spec.gain,
        }
        return ref, chan, spec.sample_rate, meta

    if mode != "file":
        raise ValueError(f"source.mode 必须是 'synthetic' 或 'file'，收到 {mode!r}")

    rel_path = source.get("path")
    if not isinstance(rel_path, str) or not rel_path:
        raise ValueError("source.path 必须为非空字符串")
    # 路径相对请求文件所在目录解析，避免依赖调用方当前工作目录。
    path = (base_dir / rel_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"找不到输入文件: {path}")

    kind = source.get("kind", "raw_pcm")
    max_samples = source.get("max_samples")
    ref_idx = int(source.get("ref_channel", 0))
    chan_idx = int(source.get("channel", 1))

    if kind == "wav":
        ref, sr_ref = read_wav(str(path), ref_idx, max_samples)
        chan, sr_chan = read_wav(str(path), chan_idx, max_samples)
        sample_rate = sr_ref  # 两次读取同一文件，采样率必然一致
    elif kind == "raw_pcm":
        sample_rate = source.get("sample_rate")
        if sample_rate is None:
            raise ValueError("raw_pcm 输入必须在 source.sample_rate 中提供采样率")
        channels = int(source.get("channels", 2 if chan_idx != ref_idx else 1))
        dtype = str(source.get("dtype", "s16"))
        ref, _ = read_raw_pcm(
            str(path), dtype, channels, ref_idx, sample_rate, max_samples
        )
        chan, _ = read_raw_pcm(
            str(path), dtype, channels, chan_idx, sample_rate, max_samples
        )
    else:
        raise ValueError(f"不支持的 source.kind {kind!r}（可选 raw_pcm / wav）")

    if ref.size != chan.size:
        n = min(ref.size, chan.size)
        ref, chan = ref[:n], chan[:n]
    meta = {
        "mode": "file",
        "kind": kind,
        "path": str(path),
        "sample_rate": sample_rate,
    }
    return ref, chan, sample_rate, meta


def _summarize(window_results, sample_rate, config: EstimatorConfig) -> dict:
    confident = [w for w in window_results if w.estimate.confident]
    delays = np.array([w.estimate.delay_samples for w in confident], dtype=np.float64)

    if confident:
        spread = float(delays.max() - delays.min())
        if spread <= config.min_peak_distance:
            consensus = "stable"
        else:
            consensus = "varying"
        summary = {
            "n_windows": len(window_results),
            "n_confident": len(confident),
            "n_uncertain": len(window_results) - len(confident),
            "consensus": consensus,
            "delay_samples_mean": _finite(float(delays.mean())),
            "delay_samples_spread": _finite(spread),
            "delay_seconds_mean": _finite(float(delays.mean()) / sample_rate)
            if sample_rate
            else None,
            "all_confident": len(confident) == len(window_results) and len(window_results) > 0,
        }
    else:
        summary = {
            "n_windows": len(window_results),
            "n_confident": 0,
            "n_uncertain": len(window_results),
            "consensus": "none",
            "delay_samples_mean": None,
            "delay_samples_spread": None,
            "delay_seconds_mean": None,
            "all_confident": False,
        }
    return summary


def run_analysis(
    request: dict,
    base_dir: Path | str,
    output_dir: Path | str,
) -> dict:
    """执行一次完整分析并写出文件。

    返回可直接 JSON 序列化的结果字典，同时写出：
      * ``results.json``  —— 完整数值结果；
      * ``correlation.npz`` —— 每窗 NCC 曲线与 lag 轴（供后续脚本复核）；
      * ``signals.npz``  —— 实际参与计算的两通道信号（合成模式便于回放验证，
        文件模式默认也保存，方便离线复查）。
    """
    if not isinstance(request, dict):
        raise ValueError("请求必须是 JSON 对象")
    base_dir = Path(base_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    config = EstimatorConfig.from_dict(request.get("estimator", {}))
    ref, chan, sample_rate, source_meta = _load_signals(
        request.get("source", {}), base_dir
    )
    if ref.size <= 2 * config.max_lag:
        raise ValueError(
            f"信号长度 ({ref.size}) 必须大于 2*max_lag ({2 * config.max_lag})"
        )

    window_results = estimate_windows(ref, chan, config)
    if not window_results:
        raise ValueError("未能切出任何完整窗口（检查 window_size 与信号长度）")

    lags = np.arange(-config.max_lag, config.max_lag + 1)
    ncc_rows = np.stack(
        [
            normalized_cross_correlation(
                ref[w.start : w.end], chan[w.start : w.end],
                config.max_lag, config.subtract_mean,
            )
            for w in window_results
        ]
    )

    result = {
        "schema_version": 1,
        "source": source_meta,
        "parameters": {
            "max_lag": config.max_lag,
            "min_peak": config.min_peak,
            "max_secondary_peak_ratio": config.max_secondary_peak_ratio,
            "min_rms_db": config.min_rms_db,
            "min_peak_distance": config.min_peak_distance,
            "window_size": config.window_size,
            "hop_size": config.hop_size,
            "subtract_mean": config.subtract_mean,
            "interpolate": config.interpolate,
        },
        "summary": _summarize(window_results, sample_rate, config),
        "windows": [w.to_dict(sample_rate) for w in window_results],
        "sign_convention": (
            "delay_samples > 0: chan 相对 ref 右移（chan 滞后）；"
            "NCC 比较 ref[k-d] 与 chan[k]，峰位即右移量"
        ),
    }

    json_path = output_dir / "results.json"
    npz_path = output_dir / "correlation.npz"
    sig_path = output_dir / "signals.npz"
    _check_not_exist(json_path, npz_path, sig_path)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")
    np.savez(npz_path, lags=lags, ncc=ncc_rows)
    np.savez(
        sig_path,
        ref=ref,
        chan=chan,
        sample_rate=np.array(sample_rate if sample_rate is not None else -1),
    )
    result["_output_files"] = {
        "results_json": str(json_path),
        "correlation_npz": str(npz_path),
        "signals_npz": str(sig_path),
    }
    return result


def _check_not_exist(*paths: Path) -> None:
    existing = [str(p) for p in paths if p.exists()]
    if existing:
        raise FileExistsError(f"输出文件已存在，拒绝覆盖: {existing}")


def _finite(x: float) -> float | None:
    return float(x) if np.isfinite(x) else None
