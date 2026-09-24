"""HTTP-level tests for the FastAPI service."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.archive_guard.limits import Limits
from app.config import Settings, set_settings
from tests.tarbuild import make_tar


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        store_dir=str(tmp_path / "store"),
        spool_dir=str(tmp_path / "spool"),
        limits=Limits(
            max_upload_bytes=1024 * 1024,
            max_entries=20,
            max_total_size=4096,
            max_total_written_bytes=4096,
            max_single_file_bytes=1024,
        ),
    )
    set_settings(settings)
    from app.main import app

    return TestClient(app)


def _post(client, url, data: bytes, filename="a.tar"):
    return client.post(url, files={"file": (filename, data, "application/x-tar")})


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["limits"]["max_entries"] == 20


def test_inspect_benign(client):
    data = make_tar([
        {"name": "d/", "kind": "dir"},
        {"name": "d/a.txt", "kind": "file", "payload": b"hello"},
    ])
    r = _post(client, "/api/v1/inspect", data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["safe"] is True
    assert body["entry_count"] == 2
    assert body["kind_counts"]["file"] == 1
    assert body["total_declared_bytes"] == 5
    assert len(body["sha256"]) == 64


def test_inspect_rejects_traversal(client):
    data = make_tar([{"name": "../evil", "kind": "file", "payload": b"x"}])
    r = _post(client, "/api/v1/inspect", data)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "unsafe_archive"
    assert "traversal" in body["message"]
    assert body["member"] == "../evil"


def test_inspect_rejects_symlink_escape(client):
    data = make_tar([
        {"name": "sub/up", "kind": "symlink", "target": "../../../"},
        {"name": "sub/up/x", "kind": "file", "payload": b"x"},
    ])
    r = _post(client, "/api/v1/inspect", data)
    assert r.status_code == 422
    assert r.json()["error"] == "unsafe_archive"


def test_inspect_rejects_device(client):
    import tarfile

    data = make_tar([{"name": "dev", "kind": "device",
                      "typeflag": tarfile.CHRTYPE}])
    r = _post(client, "/api/v1/inspect", data)
    assert r.status_code == 422


def test_inspect_rejects_garbage(client):
    r = _post(client, "/api/v1/inspect", b"not a tar at all")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_archive"


def test_upload_size_limit(client):
    big = b"\x00" * (2 * 1024 * 1024)
    r = _post(client, "/api/v1/inspect", big)
    assert r.status_code == 413
    assert r.json()["error"] == "quota_exceeded"


def test_extract_success_publishes_directory(client, tmp_path):
    data = make_tar([
        {"name": "hello.txt", "kind": "file", "payload": b"world"},
    ])
    r = _post(client, "/api/v1/extract", data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["written_bytes"] == 5
    published = Path(body["path"])
    assert published.is_dir()
    assert (published / "hello.txt").read_bytes() == b"world"
    # It lives under the configured store.
    assert str(published).startswith(str(tmp_path / "store"))


def test_extract_failure_publishes_nothing(client, tmp_path):
    data = make_tar([
        {"name": "ok.txt", "kind": "file", "payload": b"ok"},
        {"name": "../escape.txt", "kind": "file", "payload": b"PWNED"},
    ])
    before = set(os.listdir(tmp_path / "store"))
    r = _post(client, "/api/v1/extract", data)
    assert r.status_code == 422
    after = set(os.listdir(tmp_path / "store"))
    assert before == after  # no final dir, no staging leftovers
    # Nothing escaped beside the store either.
    assert not (tmp_path / "escape.txt").exists()


def test_extract_quota_exceeded(client):
    data = make_tar([
        {"name": "big.bin", "kind": "file", "payload": b"A" * 5000},
    ])
    r = _post(client, "/api/v1/extract", data)
    assert r.status_code == 413
    body = r.json()
    assert body["error"] == "quota_exceeded"
    assert body["actual"] >= 5000


def test_openapi_docs_available(client):
    r = client.get("/docs")
    assert r.status_code == 200


def test_missing_file_field(client):
    r = client.post("/api/v1/inspect")
    assert r.status_code == 422
