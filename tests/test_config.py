"""Tests for configuration and error helpers."""

import pytest

from sym_eig.config import Settings
from sym_eig.errors import EigServiceError, ErrorCategory


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("SYMEIG_MAX_N", "77")
    monkeypatch.setenv("SYMEIG_RESIDUAL_RTOL", "1.25e-7")
    settings = Settings.from_env()
    assert settings.max_n == 77
    assert settings.residual_rtol == pytest.approx(1.25e-7)
    # Unrelated fields keep defaults.
    assert settings.symmetry_atol == Settings().symmetry_atol


def test_settings_frozen():
    with pytest.raises(Exception):
        Settings().max_n = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.INVALID_MATRIX,
        ErrorCategory.NON_SYMMETRIC,
        ErrorCategory.SIZE_LIMIT_EXCEEDED,
        ErrorCategory.NON_CONVERGENCE,
    ],
)
def test_classified_errors_map_to_422(category):
    err = EigServiceError(category, "boom")
    assert err.http_status == 422
    assert err.category is category
    assert err.message == "boom"
