"""pytest 公共夹具：所有测试使用独立临时目录（文件库 + 种子文件 + 日志）。

并发与恢复测试*必须*用文件库（WAL），不用内存库。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

from app.config import DEFAULT_SYNTHETIC_TOKENS, Settings
from app.core.seed import SecretSeed
from app.repository_support import make_service

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 与 app.reproducibility.fixtures 相同的两颗固定种子（测试内自行构造，
# 不经过被测夹具模块，以保持期望值来源独立）
SEED_A = b"rct-fixed-seed-v1-A".ljust(32, b"0")
SEED_B = b"rct-fixed-seed-v1-B".ljust(32, b"0")
SEED_A_B64 = base64.b64encode(SEED_A).decode()
SEED_B_B64 = base64.b64encode(SEED_B).decode()


@pytest.fixture
def runtime(tmp_path):
    rt = make_service(
        db_path=str(tmp_path / "test.db"),
        seeds_path=str(tmp_path / "seeds.json"),
        log_path=str(tmp_path / "audit.log"),
    )
    rt["tmp_path"] = tmp_path
    return rt


@pytest.fixture
def client(tmp_path):
    """隔离配置的 FastAPI TestClient（文件库 + 种子文件 + 日志）。"""
    from fastapi.testclient import TestClient

    settings = Settings(
        database_path=str(tmp_path / "api.db"),
        seeds_path=str(tmp_path / "seeds.json"),
        tokens=dict(DEFAULT_SYNTHETIC_TOKENS),
        log_path=str(tmp_path / "audit.log"),
        pbkdf2_iterations=200_000, env="test",
    )
    from app.api.app import create_app
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, settings, tmp_path


def make_two_arm_study(service, study_id="TV-STUDY", tail="keep_open",
                       seed_b64=SEED_A_B64):
    return service.create_study(
        contract_kwargs=dict(
            study_id=study_id,
            arm_specs=[("control", 1), ("treatment", 1)],
            factor_specs=[("center", ["C1", "C2", "C3"]),
                          ("stage", ["early", "late"])],
            block_multiple=2,
            tail_policy=tail,
        ),
        seed=SecretSeed.from_base64(seed_b64),
        actor_role="investigator", request_id="test-create",
    )
