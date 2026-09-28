"""Contract/config and storage tests."""

from __future__ import annotations

import pytest

from ipwate.config import load_config
from ipwate.errors import StorageError, ValidationError
from ipwate.statcontract import StatisticalContract
from ipwate.storage import load_run, save_run


def test_default_config_loads_and_contract_is_explicit():
    cfg = load_config()
    contract = StatisticalContract.from_config(cfg)
    assert contract.estimand == "ate"
    assert contract.weight_type == "stabilized"
    assert "formula" in contract.weight_definition
    # Assumptions are carried verbatim, including the non-testability warning.
    assert "NOT testable" in contract.assumptions["conditional_exchangeability"]
    assert contract.positivity_eps == 1e-6
    assert contract.clipping_enabled is False


def test_only_whitelisted_overrides_are_accepted():
    cfg = load_config()
    updated = cfg.with_api_overrides({"estimand": "att", "crossfit.n_splits": 3})
    assert updated.estimand == "att"
    assert updated.crossfit.n_splits == 3
    with pytest.raises(ValidationError) as exc:
        cfg.with_api_overrides({"weights.positivity_eps": 0.5})
    assert exc.value.code == "validation_error"
    assert "weights.positivity_eps" in exc.value.details["unknown"]


def test_bad_override_values_rejected():
    cfg = load_config()
    with pytest.raises(ValidationError):
        cfg.with_api_overrides({"estimand": "median"})
    with pytest.raises(ValidationError):
        cfg.with_api_overrides({"crossfit.n_splits": 1})


def test_clipping_contract_records_trimmed_estimand():
    cfg = load_config().with_api_overrides({"weights.clipping.enabled": True})
    contract = StatisticalContract.from_config(cfg)
    assert contract.clipping_enabled is True
    assert contract.clipping_profile == cfg.weights.clipping.profile
    assert "trimmed" in contract.estimand_note.lower()


def test_storage_roundtrip(tmp_path):
    from ipwate.synthetic import generate_synthetic
    from ipwate.pipeline import run_ipw

    db = tmp_path / "runs.sqlite3"
    data = generate_synthetic(n=300, scenario="good_overlap", seed=2)
    result = run_ipw(data.x, data.a, data.y, request_id="persist-1")
    save_run(str(db), result)
    loaded = load_run(str(db), "persist-1")
    assert loaded is not None
    assert loaded["request_id"] == "persist-1"
    assert loaded["verdict"] == result.verdict
    assert loaded["estimate"]["point"] == result.estimate.point
    assert load_run(str(db), "does-not-exist") is None


def test_storage_reports_storage_error_on_bad_location():
    with pytest.raises(StorageError):
        # A path whose parent is an existing file cannot be created as a dir.
        blocker = None
        import tempfile, os
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            save_run(f"{path}/sub/db.sqlite", _DummyResult())
        finally:
            os.remove(path)


class _DummyResult:
    request_id = "x"
    verdict = "accept"
    n_observations = 1

    class _Contract:
        estimand = "ate"

    contract = _Contract()

    def to_json(self) -> str:
        return "{}"
