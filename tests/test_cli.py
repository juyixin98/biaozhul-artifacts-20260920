"""End-to-end CLI smoke tests using python -m enrange."""

import subprocess
import sys
from pathlib import Path

OID = "cd" * 16


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "enrange", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_keygen_put_stat_get_range(tmp_path):
    data_dir = tmp_path / "data"
    infile = tmp_path / "in.bin"
    payload = bytes((i * 13 + 5) % 256 for i in range(137))
    infile.write_bytes(payload)

    p = _run("keygen", "--data-dir", str(data_dir))
    assert p.returncode == 0, p.stderr

    p = _run(
        "put", "--data-dir", str(data_dir), "--id", OID,
        "--in", str(infile), "--block-size", "32",
    )
    assert p.returncode == 0, p.stderr
    assert "'blocks': 5" in p.stdout

    p = _run("stat", "--data-dir", str(data_dir), "--id", OID)
    assert p.returncode == 0
    assert "'length': 137" in p.stdout

    out = tmp_path / "tail.bin"
    p = _run(
        "get", "--data-dir", str(data_dir), "--id", OID,
        "--start", "100", "--end", "137", "--out", str(out),
    )
    assert p.returncode == 0, p.stderr
    assert out.read_bytes() == payload[100:137]
    assert "wrote 37 bytes" in p.stdout

    p = _run("list", "--data-dir", str(data_dir))
    assert OID in p.stdout


def test_cli_detects_tampering(tmp_path):
    data_dir = tmp_path / "data"
    infile = tmp_path / "in.bin"
    infile.write_bytes(b"secret" * 10)
    _run("keygen", "--data-dir", str(data_dir))
    p = _run(
        "put", "--data-dir", str(data_dir), "--id", OID,
        "--in", str(infile), "--block-size", "16",
    )
    assert p.returncode == 0

    obj = data_dir / "objects" / f"{OID}.bin"
    raw = bytearray(obj.read_bytes())
    raw[49] ^= 0x01
    obj.write_bytes(raw)

    p = _run("get", "--data-dir", str(data_dir), "--id", OID)
    assert p.returncode == 4
    assert "AUTHENTICATION FAILED" in p.stderr
    assert p.stdout == ""


def test_cli_bad_range_exit_code(tmp_path):
    data_dir = tmp_path / "data"
    infile = tmp_path / "in.bin"
    infile.write_bytes(b"abc")
    _run("keygen", "--data-dir", str(data_dir))
    _run("put", "--data-dir", str(data_dir), "--id", OID, "--in", str(infile))
    p = _run(
        "get", "--data-dir", str(data_dir), "--id", OID,
        "--start", "2", "--end", "9",
    )
    assert p.returncode == 5
