"""Evidence layer: error metrics, memory scale, and run-identity logging.

Every verification run gets a UUID, records the runtime versions, and appends
JSON-lines records so each judgement can be traced back to its inputs (via
SHA-256 digests) and its decision basis (tolerances and measured errors).
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import mpmath
import numpy as np
import scipy


def runtime_metadata() -> dict:
    """Versions of everything that can influence a numerical result."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "mpmath": mpmath.__version__,
    }


def new_run_id() -> str:
    return uuid.uuid4().hex


def array_digest(a: np.ndarray) -> str:
    """SHA-256 over shape, dtype and bytes — enough to re-identify an input."""
    a = np.ascontiguousarray(a)
    h = hashlib.sha256()
    h.update(str(a.shape).encode())
    h.update(str(a.dtype).encode())
    h.update(a.tobytes())
    return h.hexdigest()


@dataclass
class ErrorMetrics:
    """Measured deviation between an actual and an expected result."""

    max_abs_err: float
    max_rel_err: float
    rel_l2_err: float
    rtol: float
    atol: float
    passed: bool

    def to_dict(self) -> dict:
        return asdict(self)


def compute_error_metrics(
    actual: np.ndarray, expected: np.ndarray, rtol: float, atol: float
) -> ErrorMetrics:
    """Compare arrays elementwise; ``passed`` follows numpy.allclose semantics."""
    actual = np.asarray(actual, dtype=np.complex128)
    expected = np.asarray(expected, dtype=np.complex128)
    diff = np.abs(actual - expected)
    denom = np.abs(expected)
    max_abs = float(diff.max()) if diff.size else 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.where(denom > 0, diff / np.where(denom > 0, denom, 1.0), 0.0)
    max_rel = float(rel.max()) if rel.size else 0.0
    norm_e = float(np.linalg.norm(expected))
    rel_l2 = float(np.linalg.norm(diff)) / norm_e if norm_e > 0 else float(np.linalg.norm(diff))
    passed = bool(np.allclose(actual, expected, rtol=rtol, atol=atol))
    return ErrorMetrics(max_abs, max_rel, rel_l2, rtol, atol, passed)


def memory_report(m: int, n: int, L: int, k: int, mode: str, config) -> dict:
    """Byte-level memory scale of one kernel application vs. the dense matrix."""
    real_is = np.dtype(config.real_dtype).itemsize
    cplx_is = np.dtype(config.complex_dtype).itemsize
    spectrum_items = (L // 2 + 1) if mode == "real" else L
    in_is = real_is if mode == "real" else cplx_is
    out_is = in_is
    fft_bytes = {
        "circulant_column": L * in_is,
        "spectrum": spectrum_items * cplx_is,
        "padded_input": L * k * in_is,
        "output": m * k * out_is,
    }
    return {
        "embedding_length": L,
        "batch": k,
        "fft_workspace_bytes": fft_bytes,
        "fft_total_bytes": sum(fft_bytes.values()),
        "dense_matrix_bytes": m * n * in_is,
        "dense_to_fft_ratio": (m * n * in_is) / max(1, sum(fft_bytes.values())),
    }


class EvidenceLogger:
    """Append-only JSONL evidence log tied to one run identity."""

    def __init__(self, path: str | Path, run_id: str | None = None) -> None:
        self.path = Path(path)
        self.run_id = run_id or new_run_id()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, step: str, **fields) -> dict:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "step": step,
            **fields,
        }
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()
        return record

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "EvidenceLogger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
