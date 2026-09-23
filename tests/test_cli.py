"""CLI 端到端测试（subprocess 方式真实执行命令）。"""

from __future__ import annotations

import os
import subprocess
import sys


def run_cli(*args, cwd=None, check=True):
    proc = subprocess.run(
        [sys.executable, "-m", "envelope_enc.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"命令失败: {args}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def test_full_cli_workflow(tmp_path):
    kr = str(tmp_path / "keys.json")
    plain = tmp_path / "secret.txt"
    enc = tmp_path / "secret.txt.enc"
    dec = tmp_path / "secret.out.txt"
    plain.write_bytes(b"hello envelope encryption\n" * 50)

    # 初始化密钥环
    p = run_cli("keyring-init", "-k", kr)
    assert "active" in p.stdout

    # 加密 + inspect
    run_cli("encrypt", "-k", kr, "-i", str(plain), "-o", str(enc), "-c", "128")
    assert enc.exists()
    p = run_cli("inspect", str(enc))
    assert "chunk_count" in p.stdout

    # 主密钥轮换（旧 -> retired）
    p = run_cli("rotate-master", "-k", kr)
    assert "retired" in p.stdout

    # 文件轮换 + 解密
    p = run_cli("rotate", "-k", kr, str(enc))
    assert "已轮换" in p.stdout
    run_cli("decrypt", "-k", kr, "-i", str(enc), "-o", str(dec))
    assert dec.read_bytes() == plain.read_bytes()

    # 再次轮换：幂等跳过
    p = run_cli("rotate", "-k", kr, str(enc))
    assert "跳过" in p.stdout

    # keyring-ls 能看到两代
    p = run_cli("keyring-ls", "-k", kr)
    assert "retired" in p.stdout and "active" in p.stdout


def test_cli_tampered_file_returns_nonzero(tmp_path):
    kr = str(tmp_path / "keys.json")
    plain = tmp_path / "p"
    enc = tmp_path / "p.enc"
    dec = tmp_path / "p.out"
    plain.write_bytes(b"x" * 500)
    run_cli("keyring-init", "-k", kr)
    run_cli("encrypt", "-k", kr, "-i", str(plain), "-o", str(enc), "-c", "32")

    # 篡改一个字节
    data = bytearray(enc.read_bytes())
    data[-1] ^= 0xFF
    enc.write_bytes(bytes(data))

    p = run_cli("decrypt", "-k", kr, "-i", str(enc), "-o", str(dec), check=False)
    assert p.returncode == 1
    assert "认证失败" in p.stderr
    assert not dec.exists()
    assert not (tmp_path / "p.out.part").exists()
