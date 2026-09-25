"""Tests for the command-line interface via main()."""

import json

import pytest

from checkpoint_service.__main__ import main


def _run(argv: list[str]) -> tuple[int, str]:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


@pytest.mark.unit
def test_cli_create_train_status_list(tmp_path: str) -> None:
    base = ["--runs-dir", str(tmp_path)]
    code, out = _run([*base, "create-run", "--run-id", "r", "--n-epochs", "1"])
    assert code == 0
    assert json.loads(out)["state"]["global_step"] == 0

    code, out = _run([*base, "train", "--run-id", "r"])
    assert code == 0
    assert json.loads(out)["finished"] is True

    code, out = _run([*base, "status", "--run-id", "r"])
    assert code == 0
    assert json.loads(out)["global_step"] == 16

    code, out = _run([*base, "inspect", "--run-id", "r"])
    assert code == 0
    info = json.loads(out)
    assert info["has_optimizer_state"] and info["has_cursor"]

    code, out = _run([*base, "list-runs"])
    assert code == 0
    assert any(r["run_id"] == "r" for r in json.loads(out)["runs"])


@pytest.mark.unit
def test_cli_unknown_run_returns_1_with_json_error(tmp_path: str) -> None:
    code, out = _run(["--runs-dir", str(tmp_path), "status", "--run-id", "ghost"])
    assert code == 1
    assert json.loads(out)["error"] == "RunNotFoundError"


@pytest.mark.unit
def test_cli_invalid_config_returns_1(tmp_path: str) -> None:
    code, out = _run(
        ["--runs-dir", str(tmp_path), "create-run", "--run-id", "r", "--batch-size", "0"]
    )
    assert code == 1
    assert "error" in json.loads(out)
