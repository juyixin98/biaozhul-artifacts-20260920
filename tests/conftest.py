"""Integration fixtures: spin up an isolated Anvil, deploy contracts, expose
an httpx client bound to the FastAPI app and a raw LocalChain handle.

Environment variables (RPC_URL / CHAIN_ID / DEPLOYMENT_FILE) are set BEFORE the
application modules are imported so that no importlib.reload is needed —
reloading would leave duplicate module-level objects (e.g. ORDER_FIELDS) with
different identities and produce wrong EIP-712 hashes.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.chain import LocalChain  # noqa: E402
from app.config import ANVIL_TEST_KEYS  # noqa: E402

ANVIL_BIN = os.getenv(
    "ANVIL_BIN",
    shutil.which("anvil") or str(Path.home() / ".foundry/bin/anvil"),
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_rpc(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = requests.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
                timeout=2,
            )
            if r.status_code == 200:
                return
        except requests.RequestException as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"anvil at {url} did not become ready: {last}")


@pytest.fixture(scope="session")
def anvil():
    port = _free_port()
    rpc = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            ANVIL_BIN, "--host", "127.0.0.1", "--port", str(port),
            "--chain-id", "31337", "--silent",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_rpc(rpc)
        yield rpc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def deployment(anvil, tmp_path_factory):
    """Deploy via scripts.deploy against the fixture anvil; set env up front."""
    dep_file = tmp_path_factory.mktemp("deploy") / "deployment.json"
    os.environ["RPC_URL"] = anvil
    os.environ["CHAIN_ID"] = "31337"
    os.environ["DEPLOYMENT_FILE"] = str(dep_file)
    env = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-m", "scripts.deploy"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"deploy failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    record = json.loads(dep_file.read_text())
    record["_file"] = str(dep_file)
    return record


@pytest.fixture(scope="session")
def chain(anvil):
    return LocalChain(anvil)


@pytest.fixture(scope="session")
def env_settings(deployment):
    # Imported for the first time AFTER env vars point at the fixture chain.
    from app import main as main_module
    main_module._state.cache_clear()
    yield main_module
    main_module._state.cache_clear()


@pytest.fixture()
def client(env_settings):
    from fastapi.testclient import TestClient
    with TestClient(env_settings.app) as c:
        yield c


@pytest.fixture()
def api(anvil, deployment, env_settings):
    return {
        "module": env_settings,
        "chain": LocalChain(anvil),
        "deployment": deployment,
        "keys": {
            "deployer": ANVIL_TEST_KEYS[0],
            "maker": ANVIL_TEST_KEYS[1],
            "taker": ANVIL_TEST_KEYS[2],
            "feeRecipient": ANVIL_TEST_KEYS[3],
        },
        "w3": Web3(Web3.HTTPProvider(anvil)),
    }
