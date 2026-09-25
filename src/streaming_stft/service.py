"""Offline processing service: one request in, numbers + files out.

A request (see :func:`run_request`) specifies either a synthetic signal or a
local PCM/``.npy`` file, STFT parameters, and a list of streaming chunk
sizes.  The service:

1. computes the **bulk** STFT and WOLA inverse transform;
2. computes the same transform **chunked** with :class:`STFTStreamer` /
   :class:`ISTFTStreamer` using every requested chunk size;
3. checks frame spectra and reconstruction against the bulk path and
   against the original signal;
4. reports reconstruction error norms, window-weight coverage and exact
   zero-weight sample positions (the reconstructibility diagnostic);
5. writes numeric artefacts (spectrogram ``.npy``, reconstructed PCM/``.npy``,
   metrics JSON).  No audio players, no UI.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .pcmio import read_signal_file, write_pcm
from .signals import SyntheticSpec, generate_signal
from .stft import (
    STFTConfig,
    WindowDiagnostic,
    build_grid,
    istft,
    stft,
    validate_config,
    window_diagnostic,
)
from .streaming import ISTFTStreamer, STFTStreamer

__all__ = [
    "ServiceRequest",
    "ChunkResult",
    "ServiceResult",
    "run_request",
    "parse_request",
]


@dataclass(frozen=True)
class ServiceRequest:
    """Validated service request."""

    config: STFTConfig
    signal: np.ndarray
    chunk_sizes: tuple[int, ...]
    output_dir: str
    output_dtype: str = "float64"
    source_description: str = "synthetic"


@dataclass(frozen=True)
class ChunkResult:
    """Bulk-vs-chunked comparison for one chunk-size schedule."""

    chunk_sizes: tuple[int, ...]
    n_frames_bulk: int
    n_frames_stream: int
    frames_bit_identical: bool
    frame_max_abs_diff: float
    rebuild_max_abs_diff: float
    rebuild_rmse: float
    zero_weight_count: int
    zero_weight_first: tuple[int, ...]


@dataclass(frozen=True)
class ServiceResult:
    """Full numeric outcome of :func:`run_request`."""

    n_samples: int
    n_frames: int
    n_bins: int
    hop: int
    nfft: int
    covered: bool
    diagnostics: str
    bulk_rebuild_max_abs: float
    bulk_rebuild_rmse: float
    bulk_rebuild_relative_rms: float
    weight_min: float
    weight_max: float
    weight_is_constant: bool
    zero_weight_count: int
    zero_weight_positions: tuple[int, ...]
    chunks: tuple[ChunkResult, ...]
    output_files: dict[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["zero_weight_positions"] = list(self.zero_weight_positions)
        return data


def _error_metrics(reconstructed: np.ndarray, original: np.ndarray) -> tuple[float, float, float]:
    diff = reconstructed - original
    max_abs = float(np.max(np.abs(diff))) if diff.size else 0.0
    rmse = float(np.sqrt(np.mean(diff * diff))) if diff.size else 0.0
    rms = float(np.sqrt(np.mean(original * original))) if original.size else 0.0
    relative = rmse / rms if rms > 0.0 else float("inf") if rmse > 0.0 else 0.0
    return max_abs, rmse, relative


def _stream_roundtrip(
    signal: np.ndarray, config: STFTConfig, chunk_sizes: tuple[int, ...]
) -> tuple[np.ndarray, np.ndarray, ISTFTStreamer]:
    """Run the chunked analysis/synthesis.

    The schedule cycles through ``chunk_sizes`` by chunk index.
    Returns concatenated spectra, rebuilt signal and the finished
    synthesizer (for zero-weight inspection).
    """
    analyzer = STFTStreamer(config)
    synthesizer = ISTFTStreamer(config)
    frame_blocks: list[np.ndarray] = []
    rebuilt_parts: list[np.ndarray] = []

    pos = 0
    index = 0
    sizes = list(chunk_sizes) or [max(1, signal.shape[0])]
    while pos < signal.shape[0]:
        size = sizes[index % len(sizes)]
        index += 1
        block = signal[pos : pos + size]
        pos += block.shape[0]
        spectra = analyzer.push(block)
        if spectra.shape[0]:
            frame_blocks.append(spectra)
            rebuilt_parts.append(synthesizer.push_frames(spectra))

    tail = analyzer.flush()
    if tail.shape[0]:
        frame_blocks.append(tail)
        rebuilt_parts.append(synthesizer.push_frames(tail))
    rebuilt_parts.append(synthesizer.flush(signal.shape[0]))

    spectra = (
        np.concatenate(frame_blocks, axis=0)
        if frame_blocks
        else np.zeros((0, config.nfft // 2 + 1), dtype=np.complex128)
    )
    rebuilt = np.concatenate(rebuilt_parts) if rebuilt_parts else np.zeros(0)
    return spectra, rebuilt, synthesizer


def run_chunk_schedule(
    signal: np.ndarray, config: STFTConfig, chunk_sizes: tuple[int, ...]
) -> ChunkResult:
    """Compare one chunk schedule against the bulk computation."""
    bulk = stft(signal, config)
    spectra, rebuilt, synthesizer = _stream_roundtrip(signal, config, chunk_sizes)

    if spectra.shape == bulk.spectrogram.shape:
        frame_max = float(np.abs(spectra - bulk.spectrogram).max()) if spectra.size else 0.0
        identical = bool(np.array_equal(spectra, bulk.spectrogram))
    else:
        frame_max = float("inf")
        identical = False

    bulk_signal = istft(bulk).signal
    if rebuilt.shape == bulk_signal.shape:
        rebuild_diff = rebuilt - bulk_signal
        max_abs = float(np.max(np.abs(rebuild_diff))) if rebuild_diff.size else 0.0
        rmse = float(np.sqrt(np.mean(rebuild_diff * rebuild_diff))) if rebuild_diff.size else 0.0
    else:
        max_abs, rmse = float("inf"), float("inf")

    zero_positions = synthesizer.zero_weight_positions
    return ChunkResult(
        chunk_sizes=tuple(int(s) for s in chunk_sizes),
        n_frames_bulk=int(bulk.n_frames),
        n_frames_stream=int(spectra.shape[0]),
        frames_bit_identical=identical,
        frame_max_abs_diff=frame_max,
        rebuild_max_abs_diff=max_abs,
        rebuild_rmse=rmse,
        zero_weight_count=int(zero_positions.size),
        zero_weight_first=tuple(int(i) for i in zero_positions[:16]),
    )


def run_request(request: ServiceRequest, *, write_files: bool = True) -> ServiceResult:
    """Execute the full bulk/chunked comparison and optionally save artefacts."""
    validate_config(request.config)
    signal = request.signal
    bulk = stft(signal, request.config)
    rebuilt = istft(bulk)
    max_abs, rmse, relative_rms = _error_metrics(rebuilt.signal, signal)

    grid, _, n_frames = build_grid(signal, request.config)
    diag: WindowDiagnostic = window_diagnostic(
        request.config, grid.shape[0], n_frames, signal.shape[0], bulk.pad_left
    )

    chunks = tuple(
        run_chunk_schedule(signal, request.config, sizes)
        for sizes in _normalize_schedules(request.chunk_sizes)
    )

    out_dir = Path(request.output_dir)
    files: dict[str, str] = {}
    if write_files:
        out_dir.mkdir(parents=True, exist_ok=True)
        spec_path = out_dir / "spectrogram.npy"
        np.save(spec_path, bulk.spectrogram)
        files["spectrogram"] = str(spec_path)

        rec_npy = out_dir / "reconstructed.npy"
        np.save(rec_npy, rebuilt.signal)
        files["reconstructed_npy"] = str(rec_npy)

        rec_pcm = out_dir / "reconstructed.pcm"
        write_pcm(rec_pcm, rebuilt.signal, dtype=request.output_dtype)
        files["reconstructed_pcm"] = str(rec_pcm)

        if signal.size:
            orig_pcm = out_dir / "input.pcm"
            write_pcm(orig_pcm, signal, dtype=request.output_dtype)
            files["input_pcm"] = str(orig_pcm)

    return ServiceResult(
        n_samples=int(signal.shape[0]),
        n_frames=int(bulk.n_frames),
        n_bins=int(request.config.nfft // 2 + 1),
        hop=int(request.config.hop),
        nfft=int(request.config.nfft),
        covered=bool(rebuilt.covered),
        diagnostics=rebuilt.diagnostics,
        bulk_rebuild_max_abs=max_abs,
        bulk_rebuild_rmse=rmse,
        bulk_rebuild_relative_rms=relative_rms,
        weight_min=float(np.min(rebuilt.weights)) if rebuilt.weights.size else 0.0,
        weight_max=float(np.max(rebuilt.weights)) if rebuilt.weights.size else 0.0,
        weight_is_constant=diag.weight_is_constant,
        zero_weight_count=int(rebuilt.zero_weight.size),
        zero_weight_positions=tuple(int(i) for i in rebuilt.zero_weight),
        chunks=chunks,
        output_files=files,
    )


def _normalize_schedules(chunk_sizes: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    """Interpret the flat chunk list as one repeating schedule."""
    if not chunk_sizes:
        return ((128,),)
    if any(size <= 0 for size in chunk_sizes):
        raise ValueError(f"chunk sizes must be positive, got {chunk_sizes}")
    return (tuple(int(s) for s in chunk_sizes),)


def parse_request(data: dict[str, Any]) -> ServiceRequest:
    """Parse a JSON-like request dictionary into a :class:`ServiceRequest`.

    Expected schema (see ``examples/``)::

        {
          "stft": {"nfft": 256, "hop": 128, "window": "hann",
                   "center": true, "pad_mode": "reflect"},
          "input": {"kind": "synthetic",
                    "signal": {"duration_s": 1.0, "sample_rate": 8000,
                               "frequencies": [440.0]}}
                   OR {"kind": "file", "path": "in.pcm", "dtype": "float64"},
          "streaming": {"chunk_sizes": [137, 363, 1, 499]},
          "output": {"dir": "out", "dtype": "float64"}
        }
    """
    stft_data = data.get("stft", {})
    config = STFTConfig(
        nfft=int(stft_data.get("nfft", 256)),
        hop=int(stft_data.get("hop", 128)),
        window=stft_data.get("window", "hann"),
        center=bool(stft_data.get("center", True)),
        pad_mode=str(stft_data.get("pad_mode", "reflect")),
        periodic_window=bool(stft_data.get("periodic_window", True)),
    )
    validate_config(config)

    input_data = data.get("input", {})
    kind = input_data.get("kind", "synthetic")
    if kind == "synthetic":
        sig_data = input_data.get("signal", {})
        spec = SyntheticSpec(
            duration_s=float(sig_data.get("duration_s", 1.0)),
            sample_rate=int(sig_data.get("sample_rate", 8000)),
            frequencies=tuple(float(f) for f in sig_data.get("frequencies", [440.0])),
            amplitudes=tuple(float(a) for a in sig_data["amplitudes"])
            if "amplitudes" in sig_data
            else None,
            noise_std=float(sig_data.get("noise_std", 0.0)),
            chirp_to=float(sig_data["chirp_to"]) if "chirp_to" in sig_data else None,
            seed=int(sig_data.get("seed", 0)),
        )
        signal = generate_signal(spec)
        source = f"synthetic({spec.n_samples()} samples @ {spec.sample_rate} Hz)"
    elif kind == "file":
        path = input_data["path"]
        dtype = str(input_data.get("dtype", "float64"))
        signal = read_signal_file(path, dtype=dtype)
        source = f"file({path}, {dtype})"
    else:
        raise ValueError(f"input.kind must be 'synthetic' or 'file', got {kind!r}")

    streaming = data.get("streaming", {})
    chunk_sizes = tuple(int(s) for s in streaming.get("chunk_sizes", [128]))

    output = data.get("output", {})
    return ServiceRequest(
        config=config,
        signal=signal,
        chunk_sizes=chunk_sizes,
        output_dir=str(output.get("dir", "out")),
        output_dtype=str(output.get("dtype", "float64")),
        source_description=source,
    )


def result_to_json(result: ServiceResult) -> str:
    """Serialize the numeric result (without file arrays) to JSON."""
    return json.dumps(result.to_json_dict(), indent=2, sort_keys=True)
