"""Evidence-layer tests: metrics on known values, memory scale, JSONL logging."""

import json

import numpy as np

from toeplitz_fft.config import ToeplitzConfig
from toeplitz_fft.evidence import (
    EvidenceLogger,
    array_digest,
    compute_error_metrics,
    memory_report,
    runtime_metadata,
)


def test_metrics_exact_match_passes_with_zero_error():
    a = np.array([1.0, 2.0, 3.0])
    m = compute_error_metrics(a, a.copy(), rtol=1e-9, atol=1e-9)
    assert m.passed is True
    assert m.max_abs_err == 0.0
    assert m.rel_l2_err == 0.0


def test_metrics_detects_known_deviation():
    actual = np.array([1.0, 2.0])
    expected = np.array([1.0, 2.1])
    m = compute_error_metrics(actual, expected, rtol=1e-9, atol=1e-9)
    assert m.passed is False
    assert abs(m.max_abs_err - 0.1) < 1e-15


def test_metrics_handles_zero_expected():
    m = compute_error_metrics(np.array([1e-15]), np.array([0.0]), rtol=1e-9, atol=1e-9)
    assert m.passed is True
    assert m.max_rel_err == 0.0


def test_memory_report_scales_with_embedding_and_dense_sizes():
    cfg = ToeplitzConfig()
    rep = memory_report(m=100, n=50, L=149, k=3, mode="real", config=cfg)
    assert rep["fft_workspace_bytes"]["circulant_column"] == 149 * 8
    assert rep["fft_workspace_bytes"]["spectrum"] == (149 // 2 + 1) * 16
    assert rep["dense_matrix_bytes"] == 100 * 50 * 8
    assert rep["fft_total_bytes"] == sum(rep["fft_workspace_bytes"].values())


def test_array_digest_is_content_sensitive():
    a = np.array([1.0, 2.0])
    b = np.array([1.0, 2.0])
    c = np.array([1.0, 2.5])
    assert array_digest(a) == array_digest(b)
    assert array_digest(a) != array_digest(c)


def test_evidence_logger_writes_traceable_jsonl(tmp_path):
    path = tmp_path / "evidence.jsonl"
    with EvidenceLogger(path, run_id="test-run-1") as log:
        log.log("case", case="demo", passed=True, err=1e-13)
        log.log("summary", total=1, failed=0)
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["run_id"] == "test-run-1"
    assert rec["step"] == "case"
    assert rec["case"] == "demo"
    assert "ts" in rec


def test_runtime_metadata_includes_versions():
    meta = runtime_metadata()
    for key in ("python", "numpy", "scipy", "mpmath"):
        assert key in meta and meta[key]
