"""Offline STFT processing service.

Runs one "job" described by a JSON request: builds a synthetic signal or reads
local raw PCM, computes the STFT both on the whole signal and in arbitrary
chunks, reconstructs both ways, and writes numerical results + files to an
output directory. No UI, no playback — files and stdout JSON only.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

import numpy as np

from .pcm_io import FORMAT_NAMES, read_pcm, write_pcm
from .signals import synthesize
from .stft import (
    STFTConfig,
    STFTError,
    cola_diagnostic,
    istft,
    number_of_frames,
    prepare_config,
    stft,
)
from .stream import StreamingISTFT, StreamingSTFT


def error_metrics(original: np.ndarray, recon: np.ndarray) -> dict:
    """RMSE / max-abs / relative-energy / SNR between two equal-length signals."""
    original = np.asarray(original, dtype=np.float64)
    recon = np.asarray(recon, dtype=np.float64)
    if original.shape != recon.shape:
        raise ValueError(
            f"shape mismatch for metrics: {original.shape} vs {recon.shape}"
        )
    if original.size == 0:
        return {
            "n_samples": 0,
            "rmse": 0.0,
            "max_abs_error": 0.0,
            "relative_error_energy": 0.0,
            "snr_db": None,
        }
    err = recon - original
    rmse = float(np.sqrt(np.mean(err**2)))
    max_abs = float(np.max(np.abs(err)))
    sig_energy = float(np.sum(original**2))
    err_energy = float(np.sum(err**2))
    rel = err_energy / sig_energy if sig_energy > 0.0 else None
    if err_energy > 0.0 and sig_energy > 0.0:
        snr = float(10.0 * np.log10(sig_energy / err_energy))
    elif sig_energy == 0.0 and err_energy == 0.0:
        snr = None  # both identically zero: error is exactly zero
    else:
        snr = float("inf") if err_energy == 0.0 else None
    return {
        "n_samples": int(original.size),
        "rmse": rmse,
        "max_abs_error": max_abs,
        "relative_error_energy": rel,
        "snr_db": snr,
    }


def _load_signal(request: dict, workdir: str) -> tuple[np.ndarray, dict]:
    src = request.get("input", {}).get("type", "synthetic")
    if src == "synthetic":
        p = request["input"]
        x = synthesize(
            length=int(p.get("length", 8000)),
            sample_rate=float(p.get("sample_rate", 8000.0)),
            frequencies=tuple(p.get("frequencies", (440.0, 1000.0))),
            amplitudes=tuple(p.get("amplitudes", (0.6, 0.3))),
            seed=int(p.get("seed", 1234)),
            noise_std=float(p.get("noise_std", 0.01)),
        )
        return x, {"type": "synthetic", "length": int(x.size)}
    if src == "pcm":
        p = request["input"]
        path = p["path"]
        if not os.path.isabs(path):
            path = os.path.join(workdir, path)
        fmt = p.get("sample_format", "s16")
        if fmt not in FORMAT_NAMES:
            raise STFTError(
                f"sample_format must be one of {FORMAT_NAMES}, got {fmt!r}"
            )
        x = read_pcm(path, fmt)
        return x, {"type": "pcm", "path": path, "sample_format": fmt,
                   "length": int(x.size)}
    raise STFTError(f"input.type must be 'synthetic' or 'pcm', got {src!r}")


def _chunk_sizes(total: int, chunks) -> list[int]:
    """Turn a chunk spec into actual per-push sizes summing to ``total``."""
    if isinstance(chunks, int) or isinstance(chunks, float):
        chunks = [int(chunks)]
    sizes: list[int] = []
    i = 0
    k = 0
    while i < total:
        size = int(chunks[k % len(chunks)])
        if size <= 0:
            raise STFTError(f"chunk sizes must be positive, got {size}")
        take = min(size, total - i)
        sizes.append(take)
        i += take
        k += 1
    return sizes


def run_job(request: dict, workdir: str | None = None) -> dict:
    """Execute a job request dict; returns the JSON-serialisable report."""
    workdir = workdir or os.getcwd()
    sp = request.get("stft", {})
    config: STFTConfig = prepare_config(
        n_fft=int(sp.get("n_fft", 256)),
        hop_length=int(sp.get("hop_length", 128)),
        window_name=str(sp.get("window", "hann")),
        center=bool(sp.get("center", True)),
        pad_mode=str(sp.get("pad_mode", "constant")),
    )

    x, input_info = _load_signal(request, workdir)
    n = x.size

    out_dir = request.get("output_dir", "output")
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(workdir, out_dir)
    os.makedirs(out_dir, exist_ok=True)
    pcm_format = request.get("output_format", "f64")
    if pcm_format not in FORMAT_NAMES:
        raise STFTError(f"output_format must be one of {FORMAT_NAMES}")

    # ---- whole-signal path ----
    spec_whole = stft(x, config)
    x_whole, info_whole = istft(spec_whole, config, signal_length=n)
    diag = cola_diagnostic(config, length=n)

    # ---- chunked (streaming) path ----
    chunk_spec = request.get("chunk_size", 100)
    sizes = _chunk_sizes(n, chunk_spec)
    streamer = StreamingSTFT(config)
    parts: list[np.ndarray] = []
    pos = 0
    chunk_frame_counts: list[int] = []
    for size in sizes:
        frames = streamer.push(x[pos : pos + size])
        parts.append(frames)
        chunk_frame_counts.append(int(frames.shape[0]))
        pos += size
    tail_frames = streamer.finish()
    parts.append(tail_frames)
    spec_stream = np.concatenate(parts, axis=0) if parts else np.empty(
        (0, config.n_fft // 2 + 1), dtype=np.complex128
    )

    # chunked inverse: feed frames one frame at a time (worst-case granularity)
    # and also in the same grouping as the forward chunks.
    istream = StreamingISTFT(config)
    for grp in parts:
        if grp.shape[0]:
            istream.push(grp)
            istream.take_ready()
    x_stream, info_stream = istream.finish(signal_length=n)

    # single-frame inverse pushes for a second granularity check
    istream1 = StreamingISTFT(config)
    for m in range(spec_whole.shape[0]):
        istream1.push(spec_whole[m : m + 1])
        istream1.take_ready()
    x_stream1, _ = istream1.finish(signal_length=n)

    # ---- metrics ----
    whole_metrics = error_metrics(x, x_whole)
    stream_metrics = error_metrics(x, x_stream)
    stream1_metrics = error_metrics(x, x_stream1)
    fwd_difference = float(
        np.max(np.abs(spec_whole - spec_stream))
        if spec_whole.size and spec_stream.size
        else (0.0 if spec_whole.shape == spec_stream.shape else float("nan"))
    )

    def _maxdiff(a, b):
        if a.shape != b.shape:
            return float("nan")
        if a.size == 0:
            return 0.0
        return float(np.max(np.abs(a - b)))

    inv_difference = _maxdiff(x_whole, x_stream)
    inv1_difference = _maxdiff(x_whole, x_stream1)

    # ---- files ----
    files: dict[str, str] = {}

    def _save_npy(name: str, arr: np.ndarray) -> str:
        path = os.path.join(out_dir, name)
        np.save(path, arr)
        files[name] = path
        return path

    def _save_raw(name: str, arr: np.ndarray) -> str:
        path = os.path.join(out_dir, name)
        arr.tofile(path)
        files[name] = path
        return path

    _save_npy("spectrum_whole.npy", spec_whole.astype(np.complex128))
    _save_npy("spectrum_stream.npy", spec_stream.astype(np.complex128))
    _save_raw("reconstructed_whole.pcm", x_whole.astype(np.float64))
    _save_raw("reconstructed_stream.pcm", x_stream.astype(np.float64))
    _save_raw("original.pcm", x.astype(np.float64))

    # machine-readable complex spectrum (real/imag CSV, first/last rows too)
    mag = np.abs(spec_whole)
    _save_npy("magnitude_whole.npy", mag.astype(np.float64))
    if spec_whole.shape[0] and request.get("write_csv", True):
        n_rows = min(int(request.get("csv_rows", 8)), spec_whole.shape[0])
        with open(os.path.join(out_dir, "spectrum_preview.csv"), "w") as fh:
            fh.write("frame,bin,real,imag,magnitude\n")
            rows = list(range(n_rows))
            if spec_whole.shape[0] > n_rows:
                rows += list(range(spec_whole.shape[0] - 2, spec_whole.shape[0]))
            for r in rows:
                for b in range(spec_whole.shape[1]):
                    z = spec_whole[r, b]
                    fh.write(
                        f"{r},{b},{z.real:.12e},{z.imag:.12e},"
                        f"{abs(z):.12e}\n"
                    )

    if pcm_format != "f64":
        write_pcm(
            os.path.join(out_dir, f"reconstructed_whole.{pcm_format}.pcm"),
            x_whole,
            pcm_format,
        )

    report = {
        "status": "ok",
        "input": input_info,
        "parameters": {
            "n_fft": config.n_fft,
            "hop_length": config.hop_length,
            "window": config.window_name,
            "center": config.center,
            "pad_mode": config.pad_mode,
            "frequency_bins": config.n_fft // 2 + 1,
        },
        "frames": {
            "expected": number_of_frames(n, config),
            "whole": int(spec_whole.shape[0]),
            "stream": int(spec_stream.shape[0]),
            "match": spec_whole.shape[0] == spec_stream.shape[0],
        },
        "chunks": {
            "sizes": sizes,
            "frames_emitted_per_chunk": chunk_frame_counts,
            "tail_frames": int(tail_frames.shape[0]),
        },
        "diagnostic": diag,
        "reconstruction": {
            "possible": bool(info_whole["reconstruction_possible"]),
            "whole": info_whole,
            "stream": {
                k: v
                for k, v in info_stream.items()
                if k in ("reconstruction_possible", "zero_weight_positions",
                         "min_weight", "n_frames")
            },
            "zero_weight_positions": info_whole["zero_weight_positions"],
        },
        "metrics": {
            "whole_vs_original": whole_metrics,
            "stream_vs_original": stream_metrics,
            "stream_singleframe_vs_original": stream1_metrics,
            "forward_whole_vs_stream_max_abs": fwd_difference,
            "inverse_whole_vs_stream_max_abs": inv_difference,
            "inverse_whole_vs_singleframe_stream_max_abs": inv1_difference,
        },
        "files": files,
    }
    with open(os.path.join(out_dir, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline STFT / inverse-STFT service. Reads a JSON job request, "
            "writes spectra, reconstructed PCM and a report JSON."
        )
    )
    parser.add_argument(
        "request",
        help="path to job request JSON (see examples/ for samples)",
    )
    args = parser.parse_args(argv)
    with open(args.request, "r") as fh:
        request = json.load(fh)
    workdir = os.path.dirname(os.path.abspath(args.request))
    try:
        report = run_job(request, workdir=workdir)
    except STFTError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
