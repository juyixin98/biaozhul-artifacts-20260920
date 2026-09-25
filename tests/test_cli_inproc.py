"""In-process CLI tests (coverage-friendly, no subprocess)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from streaming_stft import cli

pytestmark = pytest.mark.integration


def test_cli_run_in_process(tmp_path, capsys) -> None:
    request_path = tmp_path / "request.json"
    out_dir = tmp_path / "out"
    request_path.write_text(
        json.dumps(
            {
                "stft": {"nfft": 128, "hop": 32},
                "input": {
                    "kind": "synthetic",
                    "signal": {
                        "duration_s": 0.05,
                        "sample_rate": 8000,
                        "frequencies": [1000.0],
                    },
                },
                "streaming": {"chunk_sizes": [50]},
                "output": {"dir": str(out_dir)},
            }
        )
    )
    code = cli.main(["run", "--request", str(request_path)])
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["covered"] is True
    assert (out_dir / "metrics.json").exists()


def test_cli_run_unreconstructible_exit_2(tmp_path, capsys) -> None:
    request_path = tmp_path / "bad.json"
    request_path.write_text(
        json.dumps(
            {
                "stft": {"nfft": 32, "hop": 32, "center": False},
                "input": {"kind": "synthetic", "signal": {"duration_s": 0.02}},
                "output": {"dir": str(tmp_path / "o")},
            }
        )
    )
    assert cli.main(["run", "--request", str(request_path)]) == 2
    assert json.loads(capsys.readouterr().out)["covered"] is False


def test_cli_diagnose_good_and_bad(capsys) -> None:
    assert cli.main(["diagnose", "--nfft", "64", "--hop", "16"]) == 0
    good = json.loads(capsys.readouterr().out)
    assert good["interior_all_covered"] is True

    assert (
        cli.main(
            ["diagnose", "--nfft", "64", "--hop", "64", "--no-center"]
        )
        == 2
    )


def test_cli_missing_request_file(capsys) -> None:
    assert cli.main(["run", "--request", "/no/such/file.json"]) == 1
    err = capsys.readouterr().err
    assert "error" in err
