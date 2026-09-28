"""基础设施：内存库、回滚、CLI、畸形鉴权头。"""
from __future__ import annotations

import json

import pytest

from app.errors import AppError
from app.reproducibility.cli import main as cli_main
from app.storage.database import Database


def test_memory_database_shared_connection_and_rollback():
    db = Database(":memory:")
    with db.write_tx() as conn:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('k', 'v')")
    with db.connection() as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='k'").fetchone()
    assert row["value"] == "v"
    # 回滚不影响后续操作
    with pytest.raises(RuntimeError):
        with db.write_tx() as conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('k2', 'v2')")
            raise RuntimeError("boom")
    with db.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM schema_meta WHERE key='k2'"
        ).fetchone()["c"] == 0


def test_cli_writes_report_and_exit_code(tmp_path, capsys):
    out = tmp_path / "report.json"
    rc = cli_main(["--workdir", str(tmp_path / "work"), "--out", str(out)])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["overall"] == "PASS"
    err = capsys.readouterr().err
    assert "all_serial_double_runs_identical: True" in err


def test_cli_failure_exit_code(tmp_path, monkeypatch):
    # 让汇总强制失败 → 退出码 1
    from app.reproducibility import runner
    monkeypatch.setattr(runner, "_summarize",
                        lambda *a, **k: {"overall": "FAIL"})
    rc = cli_main(["--workdir", str(tmp_path / "w")])
    assert rc == 1


def test_malformed_authorization_header(client):
    c, _, _ = client
    for bad in ("Basic abc", "Bearer", "Bearer  ", "token-without-scheme"):
        r = c.get("/healthz")  # healthz 不鉴权；用需鉴权接口
        r = c.post("/v1/studies", headers={"Authorization": bad}, json={})
        assert r.status_code in (401, 403)
        if r.status_code == 401:
            assert r.json()["error"]["category"] in (
                "MISSING_CREDENTIALS", "INVALID_TOKEN")


def test_app_error_body_shape():
    err = AppError("X_CAT", 400, "msg", {"k": 1})
    body = err.to_body("req-1")
    assert body["error"] == {
        "category": "X_CAT", "message": "msg",
        "details": {"k": 1}, "request_id": "req-1"}
