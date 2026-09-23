"""测试公共辅助：确定性密钥派生、投票构造。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import crypto
from app.main import create_app

BLOCK_A = "aa" * 32
BLOCK_B = "bb" * 32
BLOCK_C = "cc" * 32


def make_validators(spec: list[tuple[str, int]]) -> tuple[list[dict], dict[str, object]]:
    """从 (id, weight) 列表确定性派生验证者；返回 (API 载荷, id->私钥)。"""
    validators = []
    keys = {}
    for vid, weight in spec:
        sk = crypto.derive_private_key(f"validator:{vid}".encode())
        validators.append(
            {"id": vid, "weight": weight, "public_key": crypto.public_key_hex(sk)}
        )
        keys[vid] = sk
    return validators, keys


def vote_payload(keys, epoch: int, validator_id: str, block_hash: str) -> dict:
    return {
        "epoch": epoch,
        "validator_id": validator_id,
        "block_hash": block_hash,
        "signature": crypto.sign_vote(keys[validator_id], epoch, block_hash),
    }


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def create_epoch(client: TestClient, epoch: int, validators: list[dict]) -> None:
    r = client.post("/epochs", json={"epoch": epoch, "validators": validators})
    assert r.status_code == 201, r.text


def post_vote(client: TestClient, payload: dict):
    return client.post("/votes", json=payload)
