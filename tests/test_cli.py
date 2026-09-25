"""CLI 端到端测试 (同进程调用 main, 捕获退出码)。"""

from pathlib import Path

import pytest

from sam.cli import main


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "trust").mkdir()
    art = tmp_path / "artifact"
    (art / "bin").mkdir(parents=True)
    (art / "bin" / "app.py").write_text(
        "import sys\nprint('artifact-ok', sys.argv[1:])\n"
    )
    (art / "data").mkdir()
    (art / "data" / "msg.txt").write_text("hello\n")
    return tmp_path


def _keygen(ws: Path) -> str:
    rc = main(
        [
            "keygen",
            "--private-key",
            str(ws / "key.pem"),
            "--public-key",
            str(ws / "trust" / "owner.pem"),
        ]
    )
    assert rc == 0
    # 私钥权限 0600
    assert (ws / "key.pem").stat().st_mode & 0o777 == 0o600
    return str(ws / "key.pem")


def _sign(ws: Path, key: str, envelope: str, entrypoint=None):
    args = [
        "sign",
        "--artifact-root",
        str(ws / "artifact"),
        "--private-key",
        key,
        "--output",
        envelope,
    ]
    if entrypoint:
        args += ["--entrypoint", entrypoint]
    return main(args)


def test_full_cli_flow(workspace: Path):
    key = _keygen(workspace)
    envelope = str(workspace / "envelope.sam.json")
    assert _sign(workspace, key, envelope, entrypoint="bin/app.py") == 0

    rc = main(
        [
            "verify",
            "--artifact-root",
            str(workspace / "artifact"),
            "--envelope",
            envelope,
            "--trust-dir",
            str(workspace / "trust"),
        ]
    )
    assert rc == 0


def test_verify_json_report(workspace: Path):
    key = _keygen(workspace)
    envelope = str(workspace / "env.json")
    _sign(workspace, key, envelope)
    # 篡改文件
    (workspace / "artifact" / "data" / "msg.txt").write_text("changed")
    rc = main(
        [
            "verify",
            "--json",
            "--artifact-root",
            str(workspace / "artifact"),
            "--envelope",
            envelope,
            "--trust-dir",
            str(workspace / "trust"),
        ]
    )
    assert rc == 2


def test_verify_failure_exit_code(workspace: Path, capsys):
    key = _keygen(workspace)
    envelope = str(workspace / "env.json")
    _sign(workspace, key, envelope)
    (workspace / "artifact" / "extra").write_text("x")
    rc = main(
        [
            "verify",
            "--artifact-root",
            str(workspace / "artifact"),
            "--envelope",
            envelope,
            "--trust-dir",
            str(workspace / "trust"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "EXTRA_FILE" in err


def test_run_executes_after_verification(workspace: Path, capfd):
    key = _keygen(workspace)
    envelope = str(workspace / "env.json")
    _sign(workspace, key, envelope, entrypoint="bin/app.py")
    rc = main(
        [
            "run",
            "--artifact-root",
            str(workspace / "artifact"),
            "--envelope",
            envelope,
            "--trust-dir",
            str(workspace / "trust"),
            "--python",
            "--",
            "arg1",
        ]
    )
    assert rc == 0
    out = capfd.readouterr().out
    assert "artifact-ok ['arg1']" in out


def test_run_blocked_when_tampered(workspace: Path):
    key = _keygen(workspace)
    envelope = str(workspace / "env.json")
    _sign(workspace, key, envelope, entrypoint="bin/app.py")
    (workspace / "artifact" / "data" / "msg.txt").write_text("tampered")
    rc = main(
        [
            "run",
            "--artifact-root",
            str(workspace / "artifact"),
            "--envelope",
            envelope,
            "--trust-dir",
            str(workspace / "trust"),
            "--python",
        ]
    )
    assert rc == 3  # 验证抛错 -> 用法/环境错误退出码, 制品没有被执行


def test_unknown_key_verify_fails(workspace: Path):
    key = _keygen(workspace)
    envelope = str(workspace / "env.json")
    _sign(workspace, key, envelope)
    # 换掉信任库
    (workspace / "trust" / "owner.pem").unlink()
    other_dir = workspace / "trust2"
    other_dir.mkdir()
    from sam.keys import generate_private_key, public_key_to_pem

    other = generate_private_key()
    (other_dir / "other.pem").write_bytes(public_key_to_pem(other.public_key()))
    rc = main(
        [
            "verify",
            "--artifact-root",
            str(workspace / "artifact"),
            "--envelope",
            envelope,
            "--trust-dir",
            str(other_dir),
        ]
    )
    assert rc == 2
