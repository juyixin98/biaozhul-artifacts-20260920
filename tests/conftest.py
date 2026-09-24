"""pytest 公共夹具：独立密钥、鉴权关闭/开启的两种应用。"""

from __future__ import annotations

import os
import uuid

import numpy as np
import pytest

# 测试进程默认关闭鉴权（直接测计算链路）；HMAC 用例自行打开
os.environ.setdefault("IK_REQUIRE_AUTH", "false")
os.environ.setdefault("IK_API_KEY", "")

from app.config import load_solver_config  # noqa: E402
from app.core.ik import solve_ik  # noqa: E402
from app.core.robot_model import RobotModel  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
def robot() -> RobotModel:
    return RobotModel.from_params()


@pytest.fixture(scope="session")
def solver_cfg():
    return load_solver_config()


@pytest.fixture
def app_no_auth():
    return create_app()


@pytest.fixture
def app_with_auth(monkeypatch):
    monkeypatch.setenv("IK_REQUIRE_AUTH", "true")
    monkeypatch.setenv("IK_API_KEY", "test-secret-key-0123456789")
    return create_app()


@pytest.fixture
def client_no_auth(app_no_auth):
    from fastapi.testclient import TestClient

    return TestClient(app_no_auth)


@pytest.fixture
def client_auth(app_with_auth):
    from fastapi.testclient import TestClient

    return TestClient(app_with_auth)


@pytest.fixture
def ik_solve(robot, solver_cfg):
    def _solve(T, current=None, extra=None):
        return solve_ik(robot, solver_cfg, np.asarray(T, float), current, extra)

    return _solve


@pytest.fixture
def fk(robot):
    return lambda q: robot.fk(np.asarray(q, dtype=float))


@pytest.fixture
def make_headers():
    """生成合法 HMAC 头的工厂。"""
    import hashlib
    import hmac
    import time as _time

    def _make(key: str, raw: bytes, *, key_id="default", ts=None, nonce=None,
              tamper=False):
        ts = str(int(_time.time())) if ts is None else str(ts)
        nonce = uuid.uuid4().hex if nonce is None else nonce
        body_sha = hashlib.sha256(raw).hexdigest()
        msg = f"{key_id}\n{ts}\n{nonce}\n{body_sha}".encode()
        sig = hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()
        if tamper:
            sig = "0" * len(sig)
        return {
            "X-IK-Key-Id": key_id,
            "X-IK-Timestamp": ts,
            "X-IK-Nonce": nonce,
            "X-IK-Signature": sig,
        }

    return _make
