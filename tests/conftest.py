"""pytest 公共 fixtures：测试密钥与已初始化信任根的客户端。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import crypto


@pytest.fixture
def keys():
    """一整套临时密钥：3 个旧根阈值成员 + 2 个制品签名者。"""

    return {
        "root": [crypto.generate_keypair() for _ in range(3)],
        "signer": [crypto.generate_keypair() for _ in range(2)],
    }


@pytest.fixture
def new_keys():
    """轮换后新根用的密钥。"""

    return {
        "root": [crypto.generate_keypair() for _ in range(2)],
        "signer": [crypto.generate_keypair() for _ in range(1)],
    }


@pytest.fixture
def client(keys, monkeypatch, tmp_path):
    """未持久化（内存态）的 TestClient。"""

    monkeypatch.delenv("STATE_PATH", raising=False)
    # main 在导入时读取环境变量，这里强制重建模块保证测试隔离。
    import importlib

    from app import main as main_module

    importlib.reload(main_module)
    return TestClient(main_module.app)


@pytest.fixture
def initialized_client(client, keys):
    """已初始化根（root_version=1, threshold=2/3, 2 个签名者）的客户端。"""

    resp = client.post(
        "/root/init",
        json={
            "root_version": 1,
            "threshold": 2,
            "threshold_public_keys": [k.public_key_hex for k in keys["root"]],
            "signer_public_keys": [k.public_key_hex for k in keys["signer"]],
        },
    )
    assert resp.status_code == 201, resp.text
    return client


def sign_envelope(signer, artifact_type: str, version: str, content: bytes):
    """客户端侧：构造一份可直接 POST /artifacts/sign 的载荷。"""

    digest = crypto.sha256_hex(content)
    nonce = crypto.generate_nonce()
    signature = crypto.sign_artifact(
        private_key_hex=signer.private_key_hex,
        digest_hex=digest,
        artifact_type=artifact_type,
        version=version,
        nonce_hex=nonce,
    )
    return {
        "artifact_type": artifact_type,
        "version": version,
        "digest": digest,
        "key_id": signer.kid,
        "nonce": nonce,
        "signature": signature,
    }


def rotate(client, old_root_keys, new_keys, new_version: int, signer_indexes, threshold=2):
    """发起一次带 threshold 个旧根批准的轮换。"""

    approvals = []
    for i in signer_indexes:
        approvals.append(
            {
                "key_id": old_root_keys[i].kid,
                "signature": crypto.sign_root_rotation(
                    private_key_hex=old_root_keys[i].private_key_hex,
                    new_root_version=new_version,
                    new_threshold=threshold,
                    new_threshold_keys=[k.public_key_hex for k in new_keys["root"]],
                    new_signer_keys=[k.public_key_hex for k in new_keys["signer"]],
                ),
            }
        )
    return client.post(
        "/root/rotate",
        json={
            "new_root_version": new_version,
            "new_threshold": threshold,
            "new_threshold_public_keys": [k.public_key_hex for k in new_keys["root"]],
            "new_signer_public_keys": [k.public_key_hex for k in new_keys["signer"]],
            "approvals": approvals,
        },
    )
