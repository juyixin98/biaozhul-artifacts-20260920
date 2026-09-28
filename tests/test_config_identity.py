"""配置加载、身份校验与杂项边界。"""
from __future__ import annotations

import json

import pytest

from app.config import load_settings
from app.core.identity import build_identity, canonical_features, features_digest
from app.contracts import build_contract
from app.core.stream import (
    StreamLocator, locate_uint32, tail_completion_pick,
)
from app.errors import AppError, ErrorCategory
from tests.conftest import SEED_A


def test_load_settings_from_file_and_env(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "database_path": str(tmp_path / "f.db"),
        "seeds_path": str(tmp_path / "f-seeds.json"),
        "log_path": str(tmp_path / "f.log"),
        "env": "from-file",
    }), encoding="utf-8")
    s = load_settings(str(cfg))
    assert s.database_path == str(tmp_path / "f.db")
    assert s.env == "from-file"
    monkeypatch.setenv("RCT_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("RCT_SEEDS", str(tmp_path / "env-seeds.json"))
    s2 = load_settings(str(cfg))
    assert s2.database_path == str(tmp_path / "env.db")  # env 覆盖文件


def test_load_settings_rejects_bad_role(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"tokens": {"tok": "superuser"}}),
                   encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(str(cfg))


def _contract():
    return build_contract(
        study_id="S", arm_specs=[("A", 1), ("B", 1)],
        factor_specs=[("f", ["x", "y"])], block_multiple=1,
        tail_policy="keep_open", seed_fingerprint="seed_" + "0" * 64,
        seed_proof="pbkdf2-sha256$1$AA$AA")


def test_identity_rejects_bad_subject_and_key():
    c = _contract()
    with pytest.raises(AppError) as ei:
        build_identity(c, subject_id="  ", features={"f": "x"},
                       idempotency_key=None)
    assert ei.value.category == ErrorCategory.REQUEST_VALIDATION_FAILED
    with pytest.raises(AppError) as ei:
        build_identity(c, subject_id="s1", features={"f": "x"},
                       idempotency_key="  ")
    assert ei.value.category == ErrorCategory.REQUEST_VALIDATION_FAILED


def test_features_digest_order_independent():
    a = {"f1": "x", "f2": "y"}
    b = {"f2": "y", "f1": "x"}
    assert canonical_features(a) == canonical_features(b)
    assert features_digest(a) == features_digest(b)


def test_stream_roll_domain_locator():
    loc = StreamLocator(
        study_id="S", contract_fingerprint="ctr_x", stratum_key="x",
        block_index=0, purpose="roll", draw_index=0)
    v = locate_uint32(SEED_A, loc)
    assert 0 <= v < 2 ** 32


def test_tail_completion_pick_returns_candidate():
    pick = tail_completion_pick(
        SEED_A, "S", "ctr_x", "x", 0, 0, [10, 20, 30])
    assert pick in (10, 20, 30)


def test_more_invalid_contracts():
    base = dict(
        study_id="S", arm_specs=[("A", 1), ("B", 1)],
        factor_specs=[("f", ["x"])], block_multiple=1,
        tail_policy="keep_open", seed_fingerprint="seed_" + "0" * 64,
        seed_proof="p")
    with pytest.raises(AppError) as ei:
        build_contract(**{**base, "arm_specs": [("", 1), ("B", 1)]})
    assert ei.value.category == "INVALID_CONTRACT"
    with pytest.raises(AppError) as ei:
        build_contract(**{**base, "arm_specs": [("A", 1), ("B", 0)]})
    assert ei.value.category == "INVALID_CONTRACT"
    with pytest.raises(AppError) as ei:
        build_contract(**{**base, "factor_specs": [("f", ["x", "x"])]})
    assert ei.value.category == "INVALID_CONTRACT"
    with pytest.raises(AppError) as ei:
        build_contract(**{**base, "factor_specs": [("", ["x"])]})
    assert ei.value.category == "INVALID_CONTRACT"
    with pytest.raises(AppError) as ei:
        build_contract(**{**base, "block_multiple": 0})
    assert ei.value.category == "INVALID_CONTRACT"
    with pytest.raises(AppError) as ei:
        build_contract(**{**base, "study_id": ""})
    assert ei.value.category == "INVALID_CONTRACT"
    with pytest.raises(AppError) as ei:
        build_contract(**{**base, "seed_fingerprint": "nope"})
    assert ei.value.category == "INVALID_CONTRACT"
