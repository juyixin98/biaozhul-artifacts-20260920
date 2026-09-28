"""Additional edge-path coverage: marginal band, no exogenous controls,
failure classification, and configuration overrides."""

from __future__ import annotations

import numpy as np
import pytest

from app.config import load_config
from app.dgp import DGPSpec
from app.diagnostics import classify_failure
from app.errors import (
    IncompatibleShapes,
    NonFiniteData,
    SingularDesign,
    UnidentifiedModel,
)
from app.kernel import estimate as kernel_estimate
from app.service import run_estimation


@pytest.mark.unit
def test_marginal_band_verdict_is_deterministic(config, build_request):
    spec = DGPSpec(n=6000, n_instruments=2, instrument_strength=0.08, seed=51)
    resp = run_estimation(build_request(spec), config)
    cd = resp.decision.key_state["cragg_donald_f"]
    cutoff = resp.decision.key_state["weak_cutoff"]
    assert cutoff <= cd < 1.2 * cutoff
    assert resp.decision.verdict.value == "accepted_with_warning"
    assert any("marginal band" in r for r in resp.decision.reasons)


@pytest.mark.unit
def test_model_without_included_exogenous_regressors(config, build_request):
    # Only the constant, one endogenous regressor, two instruments.
    spec = DGPSpec(n=4000, n_exogenous=0, n_instruments=2,
                   instrument_strength=0.8, seed=601)
    resp = run_estimation(build_request(spec), config)
    assert resp.n_exogenous == 0
    names = [c.name for c in resp.coefficients]
    assert names == ["const", "x1"]
    assert resp.decision.verdict.value in {"accepted", "accepted_with_warning"}


@pytest.mark.unit
def test_no_constant_path_matches_oracle_formula(raw_arrays):
    data = raw_arrays(DGPSpec(n=4000, n_instruments=2, seed=602))
    y, X, Z = data["y"], data["X"], data["Z"]
    res = kernel_estimate(
        y, X, np.empty((len(y), 0)), Z,
        names_endog=("x1",), names_exog=(), names_instruments=("z1", "z2"),
        add_constant=False, cov_type="conventional", alpha=0.05,
        rank_rcond=1e-9, run_overid=True, run_endogeneity=True,
    )
    # Independent formula without constant.
    Pz = Z @ np.linalg.solve(Z.T @ Z, Z.T)
    beta = np.linalg.solve(X.T @ Pz @ X, X.T @ Pz @ y)
    np.testing.assert_allclose(res.beta, beta, rtol=1e-10)
    assert [c.name for c in res.coefficients] == ["x1"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc,expected",
    [
        (UnidentifiedModel("order condition fails"), "underidentified"),
        (UnidentifiedModel("rank condition fails"), "weak_identification"),
        (UnidentifiedModel("other weird case"), "weak_identification"),
        (SingularDesign("rank deficient"), "singular_design"),
        (NonFiniteData("bad"), "non_finite_data"),
        (IncompatibleShapes("bad"), "invalid_request"),
        (ValueError("totally unexpected"), "invalid_request"),
    ],
)
def test_classify_failure_categories(exc, expected):
    category, _ = classify_failure(exc)
    assert category.value == expected


@pytest.mark.unit
def test_config_environment_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("TWOSLS_MAX_OBS", "4242")
    monkeypatch.setenv("TWOSLS_ALPHA", "0.01")
    monkeypatch.setenv("TWOSLS_DB_PATH", str(tmp_path / "env.db"))
    cfg = load_config()
    assert cfg.max_obs == 4242
    assert cfg.significance == pytest.approx(0.01)
    assert cfg.absolute_db_path == tmp_path / "env.db"
