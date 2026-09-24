"""Frozen parameters: HMAC integrity is really enforced."""
import json

import pytest

from app.main import create_app
from app.params import ParamIntegrityError, load_and_verify
from tests.conftest import PARAM_DIR


def test_signed_manifest_loads(params):
    assert params.version == "soc-estimator-params-v1.0.0"
    assert params.nominal_capacity_ah == 50.0


def test_tampered_manifest_rejected(tampered_param_dir):
    key = bytes.fromhex((PARAM_DIR / "param_key.dev.hex").read_text().strip())
    with pytest.raises(ParamIntegrityError, match="HMAC"):
        load_and_verify(tampered_param_dir, key)


def test_wrong_key_rejected(tmp_path):
    with pytest.raises(ParamIntegrityError):
        load_and_verify(PARAM_DIR, b"\x00" * 32)


def test_missing_signature_rejected(tmp_path):
    (tmp_path / "params.json").write_text((PARAM_DIR / "params.json").read_text())
    with pytest.raises(ParamIntegrityError, match="signature"):
        load_and_verify(tmp_path, b"\x00" * 32)


def test_app_refuses_to_start_with_tampered_params(tampered_param_dir, tmp_path):
    with pytest.raises(ParamIntegrityError):
        create_app(data_dir=tmp_path / "d", param_dir=tampered_param_dir)


def test_params_endpoint_reports_verified(client):
    r = client.get("/v1/params")
    assert r.status_code == 200
    body = r.json()
    assert body["signature_verified"] is True
    assert body["manifest"]["version"] == "soc-estimator-params-v1.0.0"
    # sha256 matches the frozen artifact
    frozen = (PARAM_DIR / "params.sha256").read_text().strip()
    assert body["sha256"] == frozen
