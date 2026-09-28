"""Shared fixtures."""
from __future__ import annotations

import pytest

from stratblock.config import Config
from stratblock.contract import TailPolicy, build_study_config
from stratblock.storage import Storage

TOKENS = "enrol-token:enroller|audit-token:auditor|admin-token:administrator"


@pytest.fixture()
def mem_config() -> Config:
    return Config(db_path=":memory:", master_seed=20260927, api_tokens=TOKENS)


@pytest.fixture()
def store(mem_config: Config) -> Storage:
    s = Storage(mem_config.db_path)
    yield s
    s.close()


@pytest.fixture()
def two_arm_cfg():
    return build_study_config(
        study_id="trial-X",
        arms=["control", "treatment"],
        stratification_factors=["site"],
        block_sizes=[2, 4],
        allocation_ratio=[1, 1],
        tail_policy=TailPolicy.PERMUTED,
    )


@pytest.fixture()
def balanced_cfg():
    return build_study_config(
        study_id="bp-trial",
        arms=["C", "T1", "T2"],
        stratification_factors=["age", "sex"],
        block_sizes=[3, 6],
        allocation_ratio=[1, 1, 1],
        tail_policy=TailPolicy.BALANCED_PREFIX,
    )


@pytest.fixture()
def api_config(tmp_path) -> Config:
    db = tmp_path / "api.db"
    return Config(db_path=str(db), master_seed=20260927, api_tokens=TOKENS)
