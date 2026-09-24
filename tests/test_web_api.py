"""HTTP-level tests for the FastAPI service."""
from __future__ import annotations

import io
import json
import tarfile

from fastapi.testclient import TestClient

from app.lockgate.web import app

from .conftest import make_tarball, registry_node, vendor_path

client = TestClient(app)


def _archive_bytes(files: dict[str, bytes], mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _j(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_audit_clean_upload():
    blob = make_tarball("left-pad", "1.2.3")
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"left-pad": "^1.2.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.2.3", blob),
            }}
    data = _archive_bytes({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("left-pad", "1.2.3"): blob})
    r = client.post(
        "/api/v1/audit",
        files={"bundle": ("bundle.tar.gz", data, "application/gzip")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["reproducibility"]["reproducible"] is True


def test_failed_gate_still_returns_200():
    # an audit that finds problems is still a successful HTTP call
    pkg = {"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "~1.2.0"}}
    blob = make_tarball("left-pad", "1.3.0")
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "dependencies": {"left-pad": "~1.2.0"}},
                "node_modules/left-pad": registry_node("left-pad", "1.3.0", blob),
            }}
    data = _archive_bytes({"package.json": _j(pkg), "package-lock.json": _j(lock),
                           vendor_path("left-pad", "1.3.0"): blob})
    r = client.post("/api/v1/audit",
                    files={"bundle": ("b.tar.gz", data, "application/gzip")})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_target_platform_form_fields():
    blob = make_tarball("win-x", "1.0.0")
    pkg = {"name": "app", "version": "1.0.0",
           "optionalDependencies": {"win-x": "^1.0.0"}}
    lock = {"name": "app", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "1.0.0",
                     "optionalDependencies": {"win-x": "^1.0.0"}},
                "node_modules/win-x": registry_node("win-x", "1.0.0", blob,
                                                    optional=True, os=["win32"]),
            }}
    data = _archive_bytes({
        "package.json": _j(pkg), "package-lock.json": _j(lock),
        vendor_path("win-x", "1.0.0"): blob})
    r = client.post("/api/v1/audit",
                    files={"bundle": ("b.tar.gz", data, "application/gzip")},
                    data={"os": "linux", "cpu": "x64"})
    assert r.status_code == 200
    assert r.json()["summary"]["platform"] == {"os": "linux", "cpu": "x64", "libc": None}


def test_os_without_cpu_rejected():
    data = _archive_bytes({"package.json": b"{}", "package-lock.json": b"{}"})
    r = client.post("/api/v1/audit",
                    files={"bundle": ("b.tar.gz", data, "application/gzip")},
                    data={"os": "linux"})
    assert r.status_code == 400


def test_unsafe_archive_rejected_with_422():
    evil = _archive_bytes({"../escape": b"x"})
    r = client.post("/api/v1/audit",
                    files={"bundle": ("evil.tar.gz", evil, "application/gzip")})
    assert r.status_code == 422


def test_garbage_payload_rejected():
    r = client.post("/api/v1/audit",
                    files={"bundle": ("x.tar.gz", b"not an archive at all",
                                      "application/gzip")})
    assert r.status_code == 422


def test_missing_file_field_rejected():
    r = client.post("/api/v1/audit")
    assert r.status_code == 422


def test_openapi_documents_endpoint():
    spec = client.get("/openapi.json").json()
    assert "/api/v1/audit" in spec["paths"]
