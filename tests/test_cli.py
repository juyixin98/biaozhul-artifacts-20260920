"""命令行入口 python -m drift 的冒烟测试。"""
from __future__ import annotations

import socket
import threading
import time
import urllib.request

import pytest

from drift.__main__ import main
from drift.service import run_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_run_server_serves_healthz_then_shuts_down(monkeypatch) -> None:
    port = _free_port()
    thread = threading.Thread(
        target=run_server, kwargs={"host": "127.0.0.1", "port": port}, daemon=True
    )
    thread.start()
    for _ in range(50):  # 等待监听就绪
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=0.2
            ) as resp:
                assert resp.status == 200
                break
        except OSError:
            time.sleep(0.05)
    else:  # pragma: no cover - 就绪失败时才走到
        pytest.fail("服务未在预期时间内启动")


def test_main_parses_args(monkeypatch) -> None:
    captured = {}

    def fake_run_server(host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("drift.__main__.run_server", fake_run_server)
    monkeypatch.setattr("sys.argv", ["prog", "--port", "9999"])
    main()
    assert captured == {"host": "127.0.0.1", "port": 9999}
