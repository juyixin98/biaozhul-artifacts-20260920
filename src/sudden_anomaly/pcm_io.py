"""Raw PCM input and numeric/file outputs (no audio decoding, no UI).

Supported PCM sample formats (little/big endian selectable):

    s16, s32, float32, float64, uint8

Samples are scaled to float64 in their native units (integer formats are
normalised by their full-scale magnitude so a config tuned in "sigma units"
behaves consistently across formats).  Outputs are plain numeric files:
per-sample CSV and JSON summaries — nothing graphical.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .detector import DetectionResult
from .evaluation import EvaluationReport

_DTYPES = {
    "s16": ("<i2", ">i2", 2**15),
    "s32": ("<i4", ">i4", 2**31),
    "float32": ("<f4", ">f4", 1.0),
    "float64": ("<f8", ">f8", 1.0),
    "uint8": ("u1", "u1", float(2**7)),  # offset removed before scaling
}


@dataclass(frozen=True)
class PcmInfo:
    path: str
    sample_format: str
    sample_rate: float
    n_samples: int
    endian: str


def read_pcm(
    path: str,
    *,
    sample_format: str = "s16",
    sample_rate: float = 1.0,
    endian: str = "little",
    max_samples: Optional[int] = None,
) -> tuple[np.ndarray, PcmInfo]:
    """Read a raw PCM file into a float64 1-D array.

    Multi-channel interleaved data is not assumed: files are treated as one
    channel. Integer samples are divided by full scale.
    """
    if sample_format not in _DTYPES:
        raise ValueError(
            f"unsupported sample_format {sample_format!r}; "
            f"choose from {sorted(_DTYPES)}"
        )
    little_code, big_code, full_scale = _DTYPES[sample_format]
    dtype = np.dtype(little_code if endian == "little" else big_code)
    raw = np.fromfile(path, dtype=dtype, count=max_samples if max_samples else -1)
    if raw.size == 0:
        raise ValueError(f"PCM file {path!r} contains no samples")
    x = raw.astype(np.float64)
    if sample_format == "uint8":
        x -= 128.0
    if full_scale != 1.0:
        x /= full_scale
    return x, PcmInfo(path, sample_format, sample_rate, x.size, endian)


def write_pcm_float64(path: str, x: np.ndarray) -> None:
    """Write a float64 raw PCM file (useful for round-trip fixtures)."""
    np.asarray(x, dtype="<f8").tofile(path)


def write_point_csv(
    path: str,
    result: DetectionResult,
    *,
    sample_rate: float = 1.0,
    signal: Optional[np.ndarray] = None,
) -> None:
    """Write per-sample outputs: index, time, value(optional), decision, score."""
    n = result.decisions.size
    lines = ["index,time_seconds,value,decision,score,center,scale,n_observed"]
    values = signal if signal is not None else [None] * n
    for i in range(n):
        val = values[i]
        val_s = "" if val is None or (isinstance(val, float) and np.isnan(val)) else f"{float(val):.10g}"
        score_s = "" if np.isnan(result.scores[i]) else f"{result.scores[i]:.10g}"
        center_s = "" if np.isnan(result.centers[i]) else f"{result.centers[i]:.10g}"
        scale_s = "" if np.isnan(result.scales[i]) else f"{result.scales[i]:.10g}"
        lines.append(
            f"{i},{i / sample_rate:.10g},{val_s},{result.decisions[i]},"
            f"{score_s},{center_s},{scale_s},{int(result.n_observed[i])}"
        )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_json_summary(
    path: str,
    *,
    result: DetectionResult,
    report: Optional[EvaluationReport] = None,
    config: Optional[dict] = None,
    pcm: Optional[PcmInfo] = None,
    extra: Optional[dict] = None,
) -> None:
    """Write the numeric summary of a run as JSON."""
    payload: dict = {
        "config": config or {},
        "input": (
            {
                "path": pcm.path,
                "sample_format": pcm.sample_format,
                "endian": pcm.endian,
                "sample_rate": pcm.sample_rate,
                "n_samples": pcm.n_samples,
            }
            if pcm
            else {"n_samples": int(result.decisions.size)}
        ),
        "detection": result.as_dict(),
    }
    if report is not None:
        payload["evaluation"] = report.as_dict()
    if extra:
        payload.update(extra)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def make_pcm_fixture(path: str, x: np.ndarray) -> None:
    """Test/demo helper: synthesise an s16 little-endian PCM fixture."""
    clipped = np.clip(x, -1.0, 1.0)
    np.round(clipped * 32767.0).astype("<i2").tofile(path)
