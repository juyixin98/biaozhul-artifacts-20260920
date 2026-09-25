"""CLI 入口测试。"""
from __future__ import annotations

import pytest

from pitjoin import cli


class TestCli:
    def test_demo_passes_hand_check(self, capsys):
        # Act
        rc = cli.main(["demo"])
        out = capsys.readouterr().out
        # Assert
        assert rc == 0
        assert "手算对照结果：10/10 通过" in out
        assert "LATE_INGEST" in out or "事后修订" in out

    def test_leakage_runs_and_reports_gap(self, capsys):
        # Act
        rc = cli.main(["leakage"])
        out = capsys.readouterr().out
        # Assert
        assert rc == 0
        assert "朴素 as-of" in out
        assert "事后修订泄漏" in out

    def test_unknown_command_exits(self):
        with pytest.raises(SystemExit):
            cli.main(["nonexistent"])

    def test_serve_command_starts_and_stops(self):
        # serve 在端口 0 上由 ThreadingHTTPServer 分配端口；启动后立即停止
        import threading

        from pitjoin.service import create_server

        httpd = create_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            assert httpd.server_address[1] > 0
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_run_serve_dispatches_to_service(self, monkeypatch):
        called = {}

        def fake_serve(host: str = "127.0.0.1", port: int = 8000) -> None:
            called["port"] = port

        import pitjoin.service
        monkeypatch.setattr(pitjoin.service, "serve", fake_serve)
        rc = cli.main(["serve", "--port", "9999"])
        assert rc == 0
        assert called == {"port": 9999}
