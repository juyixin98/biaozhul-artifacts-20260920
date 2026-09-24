"""pytest 公共夹具：导入路径、TestClient、测试密钥与引导辅助。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402
from app import keys as keyutil  # noqa: E402
from scripts.sighelp import make_root_body  # noqa: E402


@pytest.fixture
def app(tmp_path):
    application = create_app(persist_path=str(tmp_path / "trust-state.json"))
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def parties():
    """生成全部测试角色密钥（每次测试独立）。"""
    root_priv = [keyutil.generate_keypair()[0] for _ in range(3)]
    art_priv = [keyutil.generate_keypair()[0] for _ in range(3)]
    new_root_priv = [keyutil.generate_keypair()[0] for _ in range(3)]
    new_art_priv = [keyutil.generate_keypair()[0] for _ in range(2)]
    return {
        "root_priv": root_priv,
        "root_pub": [p.public_key() for p in root_priv],
        "art_priv": art_priv,
        "art_pub": [p.public_key() for p in art_priv],
        "new_root_priv": new_root_priv,
        "new_root_pub": [p.public_key() for p in new_root_priv],
        "new_art_priv": new_art_priv,
        "new_art_pub": [p.public_key() for p in new_art_priv],
    }


@pytest.fixture
def bootstrapped(client, parties):
    """已引导 v1 根：root 阈值 2/3，制品阈值 2/3。"""
    body = make_root_body(1, 2, 2, parties["root_pub"], parties["art_pub"])
    resp = client.post("/roots/bootstrap", json=body.model_dump())
    assert resp.status_code == 201, resp.text
    return client
