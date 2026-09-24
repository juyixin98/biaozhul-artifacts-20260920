"""End-to-end API tests: boot a real Anvil chain, deploy the contracts, and
exercise the FastAPI app over HTTP (httpx ASGI transport).

Requires `anvil` on PATH (Foundry). Everything runs locally; no real keys.
"""
from __future__ import annotations

import json
import os
import shutil

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ANVIL_PORT = 18547
RPC_URL = f"http://127.0.0.1:{ANVIL_PORT}"
# Anvil deterministic account #0 — local test key only.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _wait_for_rpc(url: str, timeout: float = 20.0) -> None:
    import urllib.request

    deadline = time.time() + timeout
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url, data=payload.encode(), headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2):
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("anvil did not start in time")


@pytest.fixture(scope="session")
def deployment(tmp_path_factory):
    if shutil.which("anvil") is None:
        pytest.skip("anvil not found on PATH")
    anvil = subprocess.Popen(
        ["anvil", "--port", str(ANVIL_PORT), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_rpc(RPC_URL)
        deploy_file = tmp_path_factory.mktemp("deploy") / "deployment.json"
        env = dict(
            os.environ,
            RPC_URL=RPC_URL,
            PRIVATE_KEY=TEST_KEY,
            DEPLOYMENT_FILE=str(deploy_file),
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "deploy_local.py")],
            env=env,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert deploy_file.exists()

        os.environ["RPC_URL"] = RPC_URL
        os.environ["PRIVATE_KEY"] = TEST_KEY
        os.environ["DEPLOYMENT_FILE"] = str(deploy_file)
        yield json.loads(deploy_file.read_text())
    finally:
        anvil.terminate()
        anvil.wait(timeout=10)


@pytest.fixture(scope="session")
def client(deployment):
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(ROOT / "backend"))
    from app.main import app  # noqa: E402

    with TestClient(app) as c:
        yield c
