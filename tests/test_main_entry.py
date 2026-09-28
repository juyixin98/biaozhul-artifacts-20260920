"""Service entrypoint wiring (without binding a real socket)."""

import amptrain.__main__ as entry


def test_main_invokes_uvicorn_with_parsed_args(monkeypatch):
    captured = {}

    def fake_run(target, host, port, reload):
        captured.update(target=target, host=host, port=port, reload=reload)

    monkeypatch.setattr("uvicorn.run", fake_run)
    argv = ["prog", "--host", "0.0.0.0", "--port", "9999"]
    monkeypatch.setattr("sys.argv", argv)
    entry.main()

    assert captured["target"] == "amptrain.api:app"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
    assert captured["reload"] is False
