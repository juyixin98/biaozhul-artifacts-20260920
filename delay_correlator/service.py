"""离线服务层：请求 JSON -> 双通道信号 -> 分块延迟估计 -> 结果 JSON。

请求示例见 ``examples/request_synthetic.json`` 与 ``examples/request_pcm.json``。

输入（input.type）
------------------
- ``synthetic``：由参数合成（noise/sine/chirp/uncorrelated/silence）；
- ``pcm``：读本地原始 PCM 或整数 PCM WAV。

输出
----
- stdout：完整结果 JSON（数值）；
- 可选 ``output.profile_npz``：各窗口相关剖面（lags/corr 数组）；
- 可选 ``output.windows_csv``：逐窗口指标表；
- 可选 ``input.save_wav`` / ``input.save_pcm``：把合成信号落盘，便于复测。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np

from .correlator import normalized_xcorr
from .estimate import estimate_window
from .pcmio import deinterleave, read_pcm, read_wav, write_pcm, write_wav
from .signals import make_delayed_pair
from .window import iter_windows

__all__ = ["run_request", "load_request", "save_json"]


def load_request(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str | Path, result: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _ms_to_samples(value_ms: float | None, sample_rate: float) -> int | None:
    if value_ms is None:
        return None
    return int(round(float(value_ms) * sample_rate / 1000.0))


def _build_signal(req: dict[str, Any], base_dir: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """返回 (a, b, signal_meta)。"""
    inp = req.get("input", {})
    itype = inp.get("type", "synthetic")

    if itype == "synthetic":
        sample_rate = float(inp.get("sample_rate", 8000.0))
        pair = make_delayed_pair(
            n=int(inp.get("n", 8000)),
            delay=int(inp.get("delay", 0)),
            signal_type=inp.get("signal_type", "noise"),
            sample_rate=sample_rate,
            freq=float(inp.get("freq", 440.0)),
            freq_end=inp.get("freq_end"),
            amplitude=float(inp.get("amplitude", 0.8)),
            snr_db=inp.get("snr_db"),
            seed=inp.get("seed", 0),
            seed_b=inp.get("seed_b"),
        )
        a, b = pair.a, pair.b
        meta = {
            "source": "synthetic",
            "signal_type": pair.signal_type,
            "true_delay_samples": pair.true_delay,
            "sample_rate": pair.sample_rate,
            "n_samples": int(a.size),
        }
        stereo = np.column_stack([a, b])
        if inp.get("save_wav"):
            write_wav(base_dir / inp["save_wav"], stereo, int(sample_rate),
                      bits=int(inp.get("save_wav_bits", 16)))
        if inp.get("save_pcm"):
            cfg = inp["save_pcm"]
            write_pcm(base_dir / cfg["path"], stereo, dtype=cfg.get("dtype", "s16"))

    elif itype == "pcm":
        dtype = inp.get("dtype", "s16")
        max_samples = inp.get("max_samples")
        mode = inp.get("mode", "interleaved")
        if mode == "wav":
            data, sr_file = read_wav(str(base_dir / inp["path"]))
            sample_rate = float(inp.get("sample_rate", sr_file))
            ch_a = int(inp.get("channel_a", 0))
            ch_b = int(inp.get("channel_b", 1))
            if max(ch_a, ch_b) >= data.shape[1]:
                raise ValueError("WAV 声道序号超出范围")
            a, b = data[:, ch_a], data[:, ch_b]
        elif mode == "interleaved":
            sample_rate = float(inp["sample_rate"])
            data = read_pcm(str(base_dir / inp["path"]), dtype=dtype, channels=2,
                            max_samples=max_samples)
            a, b = deinterleave(data)
        elif mode == "paired":
            sample_rate = float(inp["sample_rate"])
            da = read_pcm(str(base_dir / inp["path_a"]), dtype=dtype, channels=1,
                          max_samples=max_samples)[:, 0]
            db = read_pcm(str(base_dir / inp["path_b"]), dtype=dtype, channels=1,
                          max_samples=max_samples)[:, 0]
            n = min(da.size, db.size)
            a, b = da[:n], db[:n]
        else:
            raise ValueError(f"未知 pcm mode: {mode!r}")
        if max_samples is not None:
            a, b = a[: int(max_samples)], b[: int(max_samples)]
        meta = {
            "source": "pcm",
            "mode": mode,
            "sample_rate": float(sample_rate),
            "n_samples": int(a.size),
        }
    else:
        raise ValueError(f"未知 input.type: {itype!r}")

    return a, b, meta


def run_request(req: dict[str, Any], *, base_dir: str | Path = ".") -> dict[str, Any]:
    """执行一次完整的离线分析，返回可 JSON 序列化的结果字典。"""
    base_dir = Path(base_dir)
    a, b, meta = _build_signal(req, base_dir)

    cfg = req.get("analysis", {})
    sr = float(meta["sample_rate"])
    method = cfg.get("method", "zncc")

    max_lag = cfg.get("max_lag_samples")
    if max_lag is None:
        if "max_lag_ms" in cfg:
            max_lag = _ms_to_samples(cfg["max_lag_ms"], sr)
        else:
            max_lag = min(200, a.size - 1)

    win_size = cfg.get("window_size_samples")
    if win_size is None and "window_ms" in cfg:
        win_size = _ms_to_samples(cfg["window_ms"], sr)
    hop = cfg.get("hop_samples")
    if hop is None and "hop_ms" in cfg:
        hop = _ms_to_samples(cfg["hop_ms"], sr)

    min_coherence = float(cfg.get("min_coherence", 0.5))
    ambiguity_guard = int(cfg.get("ambiguity_guard_samples", 3))
    if "ambiguity_guard_ms" in cfg:
        ambiguity_guard = _ms_to_samples(cfg["ambiguity_guard_ms"], sr)
    ambiguity_ratio = float(cfg.get("ambiguity_ratio", 0.9))
    ambiguity_gap = float(cfg.get("ambiguity_gap", 0.1))
    energy_db = float(cfg.get("energy_db", -60.0))
    consistency_tol = float(cfg.get("window_consistency_samples", 1.0))

    windows = iter_windows(a.size, win_size, hop,
                           min_partial=float(cfg.get("min_partial", 0.5)))

    win_results: list[dict[str, Any]] = []
    profiles: list[tuple[np.ndarray, np.ndarray]] = []
    for w in windows:
        wa, wb = a[w.start : w.end], b[w.start : w.end]
        prof = normalized_xcorr(wa, wb, max_lag=max_lag, method=method)
        est = estimate_window(
            wa, wb, max_lag=max_lag,
            window_index=w.index, sample_start=w.start,
            sample_rate=sr, method=method,
            min_coherence=min_coherence,
            ambiguity_guard=ambiguity_guard,
            ambiguity_ratio=ambiguity_ratio,
            ambiguity_gap=ambiguity_gap,
            energy_db=energy_db,
            profile=prof,
        )
        d = est.to_dict()
        d["length_samples"] = w.length
        win_results.append(d)
        profiles.append((prof.lags, prof.corr))

    agg = _aggregate(win_results, sr, consistency_tol)
    result = {
        "signal": meta,
        "config": {
            "method": method,
            "max_lag_samples": int(min(max_lag, a.size - 1)),
            "window_size_samples": win_size if win_size is not None else int(a.size),
            "hop_samples": hop,
            "min_coherence": min_coherence,
            "ambiguity_guard_samples": ambiguity_guard,
            "ambiguity_ratio": ambiguity_ratio,
            "ambiguity_gap": ambiguity_gap,
            "energy_db": energy_db,
            "window_consistency_samples": consistency_tol,
        },
        "sign_convention": "lag > 0 表示通道 B 晚于 A（B[n] = A[n-lag]）",
        "n_windows": len(win_results),
        "windows": win_results,
        "aggregate": agg,
    }

    out_cfg = req.get("output", {})
    if out_cfg.get("profile_npz"):
        _write_profiles(base_dir / out_cfg["profile_npz"], profiles, method)
        result["outputs"] = {"profile_npz": out_cfg["profile_npz"]}
    else:
        result["outputs"] = {}
    if out_cfg.get("windows_csv"):
        _write_windows_csv(base_dir / out_cfg["windows_csv"], win_results)
        result["outputs"]["windows_csv"] = out_cfg["windows_csv"]
    return result


def _aggregate(
    win_results: list[dict[str, Any]], sr: float, consistency_tol: float
) -> dict[str, Any]:
    ok = [w for w in win_results if w["status"] == "ok"]
    reasons: list[str] = []
    bad = [w for w in win_results if w["status"] != "ok"]

    lags = np.array([w["lag_samples"] for w in ok], dtype=np.float64)
    frac = np.array(
        [w["fractional_lag"] for w in ok if w["fractional_lag"] is not None],
        dtype=np.float64,
    )

    if not ok:
        agg: dict[str, Any] = {
            "status": "uncertain",
            "uncertainty_reasons": ["all_windows_uncertain"],
            "n_ok_windows": 0,
            "lag_samples": None,
            "lag_seconds": None,
        }
        return agg

    lag_med = float(np.median(lags))
    lag_mean = float(np.mean(lags))
    lag_std = float(np.std(lags))
    spread = float(np.max(lags) - np.min(lags))
    if spread > consistency_tol:
        reasons.append("inconsistent_windows")
    if bad:
        reasons.append("some_windows_uncertain")

    coherences = np.array([w["peak_coherence"] for w in ok])
    agg = {
        "status": "uncertain" if reasons else "ok",
        "uncertainty_reasons": reasons,
        "n_ok_windows": int(len(ok)),
        "lag_samples": int(round(lag_med)),
        "lag_median_samples": lag_med,
        "lag_mean_samples": lag_mean,
        "lag_std_samples": lag_std,
        "lag_spread_samples": spread,
        "lag_seconds": lag_med / sr,
        "mean_coherence": float(np.mean(coherences)),
        "min_coherence_observed": float(np.min(coherences)),
    }
    if frac.size:
        agg["fractional_lag_median_samples"] = float(np.median(frac))
    return agg


def _write_profiles(
    path: Path, profiles: list[tuple[np.ndarray, np.ndarray]], method: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lags = np.empty(len(profiles), dtype=object)
    corr = np.empty(len(profiles), dtype=object)
    for i, (l, c) in enumerate(profiles):
        lags[i] = l
        corr[i] = c
    np.savez(path, lags=lags, corr=corr, method=np.asarray(method))


_CSV_FIELDS = [
    "window_index", "sample_start", "sample_end", "length_samples",
    "status", "lag_samples", "lag_seconds", "fractional_lag",
    "peak_coherence", "second_coherence", "peak_ratio", "prominence",
    "at_boundary", "uncertainty_reasons",
]


def _write_windows_csv(path: Path, win_results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for w in win_results:
            row = {k: w.get(k) for k in _CSV_FIELDS}
            row["uncertainty_reasons"] = ";".join(w.get("uncertainty_reasons", []))
            writer.writerow(row)
