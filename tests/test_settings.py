"""Tests for configuration loading and budget validation."""
from __future__ import annotations

import pytest

from app.settings import load_settings


def test_default_settings_load_and_budgets_are_sane(settings):
    assert settings.kernel.max_degree == 200
    assert settings.kernel.max_bisections == 20000
    assert settings.kernel.precision_dps >= 30
    assert settings.kernel.max_coeff_bits > 0
    assert settings.http.max_body_bytes > 0


def test_settings_are_immutable(settings):
    with pytest.raises(Exception):
        settings.kernel.max_degree = 9  # type: ignore[misc]


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.yaml")


def test_rejects_precision_below_floor(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        "service: {host: '127.0.0.1', port: 1, log_level: info}\n"
        "kernel:\n"
        "  precision_dps: 5\n"
        "  max_degree: 10\n"
        "  max_coefficients: 11\n"
        "  max_bisections: 100\n"
        "  max_sturm_length: 11\n"
        "  max_coeff_bits: 100\n"
        "http: {max_body_bytes: 100}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_settings(cfg)


def test_env_var_override(monkeypatch, tmp_path):
    cfg = tmp_path / "custom.yaml"
    cfg.write_text(
        "service: {host: '127.0.0.1', port: 9999, log_level: warning}\n"
        "kernel:\n"
        "  precision_dps: 55\n"
        "  max_degree: 12\n"
        "  max_coefficients: 13\n"
        "  max_bisections: 500\n"
        "  max_sturm_length: 13\n"
        "  max_coeff_bits: 4096\n"
        "http: {max_body_bytes: 2048}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ROOT_ISOLATION_CONFIG", str(cfg))
    loaded = load_settings()
    assert loaded.service.port == 9999
    assert loaded.kernel.max_degree == 12
    assert loaded.kernel.precision_dps == 55
