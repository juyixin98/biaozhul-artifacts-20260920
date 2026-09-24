"""HTTP API and CLI tests (real in-process ASGI transport, no mocking)."""

from __future__ import annotations

import io
import json

import pytest
from starlette.testclient import TestClient

from app.api import app
from tests._fixtures import make_bundle, make_tarball, manifest
from tests.test_verifier import _happy


def _client():
    return TestClient(app)


def test_healthz():
    with _client() as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_verify_endpoint_happy_path(builders):
    files = _happy(builders)
    bundle = make_bundle(files)
    with _client() as client:
        resp = client.post(
            "/api/v1/verify",
            files={"file": ("proj.tgz", bundle, "application/gzip")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pass"
    assert body["reproducibility"]["status"] is True


def test_verify_endpoint_rejects_path_traversal_archive():
    # Build a tar containing a traversal member by hand.
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("../../../../tmp/pwned.txt")
        payload = b"x"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    with _client() as client:
        resp = client.post(
            "/api/v1/verify",
            files={"file": ("evil.tgz", buf.getvalue(), "application/gzip")},
        )
    assert resp.status_code == 400
    assert "escapes" in resp.json()["detail"]


def test_verify_endpoint_platform_validation(builders):
    bundle = make_bundle(_happy(builders))
    with _client() as client:
        resp = client.post(
            "/api/v1/verify?os=plan9",
            files={"file": ("p.tgz", bundle, "application/gzip")},
        )
    assert resp.status_code == 400


def test_verify_endpoint_lock_drift_failure_returns_200_with_fail_status(builders):
    b = builders
    tar = b["make_tarball"]("left-pad", "2.0.0")
    key, node_data, vendor = b["node"]("left-pad", "2.0.0", tarball=tar)
    lock = b["lockfile3"](
        {key: node_data},
        root_extra={"dependencies": {"left-pad": "^1.0.0"}},
    )
    bundle = make_bundle(
        b["build_files"](manifest(deps={"left-pad": "^1.0.0"}), lock, [vendor])
    )
    with _client() as client:
        resp = client.post(
            "/api/v1/verify",
            files={"file": ("p.tgz", bundle, "application/gzip")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fail"
    assert any(f["code"] == "LOCK_RANGE_DRIFT" for f in body["findings"])


def test_cli_happy_path(builders, tmp_path, monkeypatch):
    from app import cli

    path = tmp_path / "p.tgz"
    path.write_bytes(make_bundle(_happy(builders)))
    rc = cli.main(["verify", str(path), "--pretty"])
    assert rc == 0


def test_cli_fail_path(builders, tmp_path, capsys, monkeypatch):
    from app import cli

    b = builders
    tar = b["make_tarball"]("left-pad", "9.0.0")
    key, node_data, vendor = b["node"]("left-pad", "9.0.0", tarball=tar)
    lock = b["lockfile3"](
        {key: node_data},
        root_extra={"dependencies": {"left-pad": "^1.0.0"}},
    )
    path = tmp_path / "p.tgz"
    path.write_bytes(
        make_bundle(
            b["build_files"](manifest(deps={"left-pad": "^1.0.0"}), lock, [vendor])
        )
    )
    rc = cli.main(["verify", str(path)])
    assert rc == 1
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "fail"


def test_cli_bad_archive(tmp_path, capsys):
    from app import cli

    path = tmp_path / "junk.tgz"
    path.write_bytes(b"nonsense")
    rc = cli.main(["verify", str(path)])
    assert rc == 2
    body = json.loads(capsys.readouterr().out)
    assert "rejected bundle" in body["error"]
