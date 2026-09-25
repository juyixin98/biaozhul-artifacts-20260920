"""CLI 冒烟测试（通过 subprocess 跑真实命令行）。"""

from __future__ import annotations

import json
import subprocess
import sys


def _run(tmp_path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "envelope", "--store", str(tmp_path / "store"), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_lifecycle(tmp_path):
    rc, out, err = _run(tmp_path, "new-key")
    assert rc == 0, err
    kid1 = json.loads(out)["kid"]

    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"command-line secret payload" * 50)

    rc, out, err = _run(tmp_path, "encrypt", str(secret), "--chunk-size", "128")
    assert rc == 0, err
    enc = json.loads(out)
    fid = enc["file_id"]
    assert enc["blocks"] >= 1

    rc, out, err = _run(tmp_path, "info", fid)
    assert rc == 0, err
    assert json.loads(out)["wrapped_by_kid"] == kid1

    recovered = tmp_path / "recovered.txt"
    rc, out, err = _run(tmp_path, "decrypt", fid, str(recovered))
    assert rc == 0, err
    assert recovered.read_bytes() == secret.read_bytes()

    rc, out, err = _run(tmp_path, "rotate", fid)
    assert rc == 0, err
    rot = json.loads(out)
    assert rot["blob_bytes_changed"] == 0 and rot["new_kid"] != kid1

    rc, out, err = _run(tmp_path, "decrypt", fid, str(recovered))
    assert rc == 0, err
    assert recovered.read_bytes() == secret.read_bytes()

    rc, out, err = _run(tmp_path, "list")
    assert rc == 0, err
    assert json.loads(out)["files"][0]["header_version"] == 2

    rc, out, err = _run(tmp_path, "list-keys")
    assert rc == 0, err
    assert len(json.loads(out)["keys"]) == 2


def test_cli_error_exit_code(tmp_path):
    rc, _out, err = _run(tmp_path, "info", "0" * 32)
    assert rc == 2
    assert "错误" in err
