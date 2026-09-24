"""Shared fixtures: compiled artifacts and a local Anvil instance."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out"


def _forge_build() -> None:
    forge = shutil.which("forge") or str(Path.home() / ".foundry/bin/forge")
    subprocess.run(
        [forge, "build"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session", autouse=True)
def compiled_artifacts():
    """Make sure `forge build` output exists before any test runs."""
    if not (OUT_DIR / "BoxV1.sol" / "BoxV1.json").exists():
        _forge_build()
    if not (OUT_DIR / "bases.json").exists():
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "export_bases.py")],
            check=True,
            capture_output=True,
            text=True,
        )
    return OUT_DIR


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def anvil():
    """Start a local Anvil node on a free port; stop it after the session."""
    binary = shutil.which("anvil") or str(Path.home() / ".foundry/bin/anvil")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("anvil did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)
