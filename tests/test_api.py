"""HTTP 接口测试（FastAPI TestClient）。"""

from __future__ import annotations

import gzip
import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.crypto import sign
from app.main import create_app
from tests.conftest import TarBuilder, tarinfo


@pytest.fixture
def client(tmp_path):
    st = Settings(
        max_upload_bytes=256 * 1024,
        max_total_bytes=64 * 1024,
        max_file_bytes=48 * 1024,
        max_entries=100,
        max_symlink_hops=8,
        max_compression_ratio=100.0,
        data_dir=str(tmp_path / "data"),
    )
    app = create_app(st)
    return TestClient(app)


def benign_tar() -> bytes:
    return (
        TarBuilder()
        .add_dir("docs")
        .add_file("docs/readme.txt", b"hello world")
        .add_symlink("docs/link", "readme.txt")
        .getvalue()
    )


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_precheck_accepts_benign(client):
    r = client.post(
        "/api/v1/archives/precheck",
        files={"archive": ("a.tar", benign_tar(), "application/x-tar")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["entry_count"] == 3
    # 预检不得写出任何解包目录
    data_dir = client.app.state.settings.data_dir
    assert os.listdir(os.path.join(data_dir, "extracted")) == []


def test_precheck_rejects_traversal(client):
    data = TarBuilder().add(tarinfo("../evil", "file", size=4), b"evil").getvalue()
    r = client.post(
        "/api/v1/archives/precheck",
        files={"archive": ("a.tar", data, "application/x-tar")},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "path_traversal"


def test_precheck_rejects_bad_format(client):
    r = client.post(
        "/api/v1/archives/precheck",
        files={"archive": ("a.tar", b"not a tar" * 50, "application/x-tar")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unreadable_archive"


def test_extract_success_and_get_manifest(client):
    r = client.post(
        "/api/v1/archives/extract",
        files={"archive": ("a.tar", benign_tar(), "application/x-tar")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ext_id = body["extraction_id"]
    assert body["bytes_written"] == len(b"hello world")
    assert body["entry_count"] == 3

    # 发布目录确实存在且文件内容正确
    data_dir = client.app.state.settings.data_dir
    pub = os.path.join(data_dir, "extracted", ext_id)
    assert os.path.isdir(pub)
    assert open(os.path.join(pub, "docs/readme.txt"), "rb").read() == b"hello world"

    r2 = client.get(f"/api/v1/extractions/{ext_id}")
    assert r2.status_code == 200
    assert r2.json()["extraction_id"] == ext_id


def test_extract_failure_is_atomic(client):
    evil = (
        TarBuilder()
        .add_file("fine.txt", b"fine")
        .add(tarinfo("../../escape", "file", size=4), b"evil")
        .getvalue()
    )
    before = set(os.listdir(os.path.join(client.app.state.settings.data_dir, "extracted")))
    r = client.post(
        "/api/v1/archives/extract",
        files={"archive": ("a.tar", evil, "application/x-tar")},
    )
    assert r.status_code == 422
    after = set(os.listdir(os.path.join(client.app.state.settings.data_dir, "extracted")))
    assert before == after  # 没有发布半成品目录
    # 隔离文件也已清理
    quar = os.path.join(client.app.state.settings.data_dir, "quarantine")
    assert all(not f.startswith("incoming-") for f in os.listdir(quar))


def test_extract_gzip(client):
    payload = TarBuilder().add_file("gz.txt", b"gzipped!").getvalue()
    gz = gzip.compress(payload)
    r = client.post(
        "/api/v1/archives/extract",
        files={"archive": ("a.tar.gz", gz, "application/gzip")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["compression"] == "gzip"


def test_upload_size_limit(client):
    big = b"x" * (client.app.state.settings.max_upload_bytes + 100)
    r = client.post(
        "/api/v1/archives/extract",
        files={"archive": ("big.bin", big, "application/octet-stream")},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "upload_too_large"


def test_empty_upload_rejected(client):
    r = client.post(
        "/api/v1/archives/extract",
        files={"archive": ("empty", b"", "application/octet-stream")},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "empty_upload"


def test_extraction_not_found(client):
    r = client.get("/api/v1/extractions/" + "f" * 32)
    assert r.status_code == 404


def test_bad_extraction_id(client):
    r = client.get("/api/v1/extractions/../../etc")
    # FastAPI 路径规则会改变形态，断言不含 200/500
    assert r.status_code in (404, 422)


# --------------------------------------------------------------------------- #
# 签名
# --------------------------------------------------------------------------- #
def test_key_generation_and_valid_signature(client):
    r = client.post("/api/v1/keys/ed25519/generate")
    assert r.status_code == 200
    keys = r.json()
    data = benign_tar()
    sig = sign(keys["private_key_pem"], data)
    compact_pem = "".join(keys["public_key_pem"].splitlines())
    r2 = client.post(
        "/api/v1/archives/extract",
        files={"archive": ("a.tar", data, "application/x-tar")},
        headers={"X-Signature": sig, "X-Public-Key": compact_pem},
    )
    assert r2.status_code == 200, r2.text


def test_invalid_signature_rejected(client):
    r = client.post("/api/v1/keys/ed25519/generate")
    keys = r.json()
    data = benign_tar()
    good = sign(keys["private_key_pem"], data)
    compact_pem = "".join(keys["public_key_pem"].splitlines())
    tampered = list(good)
    tampered[0] = "0" if good[0] != "0" else "1"
    r2 = client.post(
        "/api/v1/archives/precheck",
        files={"archive": ("a.tar", data, "application/x-tar")},
        headers={"X-Signature": "".join(tampered), "X-Public-Key": compact_pem},
    )
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "invalid_signature"


def test_signature_without_public_key_rejected(client):
    data = benign_tar()
    r = client.post(
        "/api/v1/archives/extract",
        files={"archive": ("a.tar", data, "application/x-tar")},
        headers={"X-Signature": "ab" * 64},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_public_key"


def test_quarantine_cannot_escape_via_link(client, tmp_path):
    """端到端：链接逃逸条目不得在 data 目录外留下任何文件。"""
    outside = tmp_path / "data-outside-marker"
    outside.mkdir()
    evil = TarBuilder().add_symlink("out", "../../data-outside-marker")
    evil.add(tarinfo("out/pwned", "file", size=3), b"pwn")
    r = client.post(
        "/api/v1/archives/extract",
        files={"archive": ("a.tar", evil.getvalue(), "application/x-tar")},
    )
    assert r.status_code == 422
    assert os.listdir(outside) == []
