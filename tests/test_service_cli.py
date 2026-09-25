"""End-to-end service and CLI integration tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import streaming_stft
from streaming_stft.pcmio import PCMSource, read_pcm, write_pcm
from streaming_stft.service import (
    ServiceRequest,
    parse_request,
    result_to_json,
    run_request,
)
from streaming_stft.stft import STFTConfig

_SRC = str(Path(streaming_stft.__file__).resolve().parents[1])
_ENV = {**os.environ, "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}

pytestmark = pytest.mark.integration


def test_service_synthetic_roundtrip(tmp_path) -> None:
    cfg = STFTConfig(nfft=256, hop=128, pad_mode="reflect")
    request = ServiceRequest(
        config=cfg,
        signal=np.sin(2 * np.pi * 440 * np.arange(8000) / 8000),
        chunk_sizes=(137, 363, 1, 499),
        output_dir=str(tmp_path),
    )
    result = run_request(request)
    assert result.covered
    assert result.bulk_rebuild_max_abs < 1e-10
    assert result.n_frames == result.chunks[0].n_frames_bulk
    chunk = result.chunks[0]
    assert chunk.frames_bit_identical
    assert chunk.rebuild_max_abs_diff < 1e-12
    assert chunk.zero_weight_count == 0

    metrics = json.loads((tmp_path / "metrics.json").read_text()) \
        if (tmp_path / "metrics.json").exists() else None
    # metrics.json is written by the CLI, not run_request; verify artefacts.
    spectrogram = np.load(tmp_path / "spectrogram.npy")
    assert spectrogram.shape[1] == 129
    rebuilt = np.load(tmp_path / "reconstructed.npy")
    assert rebuilt.shape == (8000,)
    pcm = read_pcm(PCMSource(str(tmp_path / "reconstructed.pcm")))
    assert pcm.shape == (8000,)


def test_service_reports_unreconstructible(tmp_path) -> None:
    cfg = STFTConfig(nfft=32, hop=32, window="hann", center=False)
    request = ServiceRequest(
        config=cfg,
        signal=np.ones(128),
        chunk_sizes=(10,),
        output_dir=str(tmp_path / "bad"),
    )
    result = run_request(request)
    assert not result.covered
    assert result.zero_weight_count > 0
    assert result.chunks[0].zero_weight_count == result.zero_weight_count
    assert "NOT RECONSTRUCTIBLE" in result.diagnostics
    # Serialization stays JSON-valid.
    parsed = json.loads(result_to_json(result))
    assert parsed["zero_weight_count"] == result.zero_weight_count


def test_parse_request_file_input(tmp_path) -> None:
    x = np.linspace(-1, 1, 500)
    pcm_path = tmp_path / "in.pcm"
    write_pcm(pcm_path, x, dtype="float32")
    payload = {
        "stft": {"nfft": 64, "hop": 16, "window": "hann", "center": True},
        "input": {"kind": "file", "path": str(pcm_path), "dtype": "float32"},
        "streaming": {"chunk_sizes": [33, 77]},
        "output": {"dir": str(tmp_path / "out")},
    }
    request = parse_request(payload)
    result = run_request(request)
    assert result.n_samples == 500
    assert result.covered
    assert result.chunks[0].frames_bit_identical


def test_parse_request_rejects_bad_chunk() -> None:
    payload = {
        "stft": {"center": True, "pad_mode": "edge"},
        "input": {"kind": "synthetic", "signal": {"duration_s": 0.01}},
        "streaming": {"chunk_sizes": [0]},
        "output": {"dir": "out"},
    }
    request = parse_request(payload)
    with pytest.raises(ValueError, match="positive"):
        run_request(request, write_files=False)


def test_cli_run_success(tmp_path) -> None:
    request_path = tmp_path / "request.json"
    out_dir = tmp_path / "out"
    request_path.write_text(
        json.dumps(
            {
                "stft": {"nfft": 128, "hop": 32, "window": "hann"},
                "input": {
                    "kind": "synthetic",
                    "signal": {
                        "duration_s": 0.05,
                        "sample_rate": 8000,
                        "frequencies": [1000.0],
                    },
                },
                "streaming": {"chunk_sizes": [50]},
                "output": {"dir": str(out_dir)},
            }
        )
    )
    proc = subprocess.run(
        [sys.executable, "-m", "streaming_stft.cli", "run", "--request",
         str(request_path)],
        cwd=None,
        capture_output=True,
        text=True,
        check=False,
        env=_ENV,
    )
    assert proc.returncode == 0, proc.stderr
    metrics = json.loads((out_dir / "metrics.json").read_text())
    assert metrics["covered"] is True
    assert metrics["bulk_rebuild_max_abs"] < 1e-9
    assert metrics["chunks"][0]["frames_bit_identical"] is True


def test_cli_run_nonzero_exit_on_unreconstructible(tmp_path) -> None:
    request_path = tmp_path / "bad.json"
    request_path.write_text(
        json.dumps(
            {
                "stft": {
                    "nfft": 32,
                    "hop": 32,
                    "window": "hann",
                    "center": False,
                },
                "input": {"kind": "synthetic", "signal": {"duration_s": 0.02}},
                "output": {"dir": str(tmp_path / "out2")},
            }
        )
    )
    proc = subprocess.run(
        [sys.executable, "-m", "streaming_stft.cli", "run", "--request",
         str(request_path)],
        capture_output=True,
        text=True,
        check=False,
        env=_ENV,
    )
    assert proc.returncode == 2
    body = json.loads(proc.stdout)
    assert body["covered"] is False


def test_cli_diagnose_exit_codes() -> None:
    good = subprocess.run(
        [sys.executable, "-m", "streaming_stft.cli", "diagnose",
         "--nfft", "64", "--hop", "16", "--window", "hann"],
        capture_output=True,
        text=True,
        check=False,
        env=_ENV,
    )
    assert good.returncode == 0
    assert json.loads(good.stdout)["interior_weight_constant"] is True

    bad = subprocess.run(
        [sys.executable, "-m", "streaming_stft.cli", "diagnose",
         "--nfft", "64", "--hop", "64", "--window", "hann", "--no-center"],
        capture_output=True,
        text=True,
        check=False,
        env=_ENV,
    )
    assert bad.returncode == 2
