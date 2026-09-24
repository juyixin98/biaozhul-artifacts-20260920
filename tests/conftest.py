"""Pytest fixtures: boot a disposable Anvil node, deploy fresh contracts for
each test, and expose a FastAPI TestClient wired to them.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from web3 import Web3

from service.app import create_app
from service.config import ANVIL_KEYS, Settings
from service.deploy import deploy_all

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _anvil_bin() -> str:
    found = shutil.which("anvil")
    if found:
        return found
    candidate = Path.home() / ".foundry" / "bin" / "anvil"
    if candidate.exists():
        return str(candidate)
    pytest.skip("anvil not installed (looked in PATH and ~/.foundry/bin)")


def _ensure_artifacts() -> None:
    if (ROOT / "out" / "MultisigTimelock.sol" / "MultisigTimelock.json").exists():
        return
    forge = shutil.which("forge") or str(Path.home() / ".foundry" / "bin" / "forge")
    subprocess.run([forge, "build"], cwd=ROOT, check=True, capture_output=True)


@pytest.fixture(scope="session")
def anvil():
    _ensure_artifacts()
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [_anvil_bin(), "--port", str(port), "--chain-id", "31337",
         "--accounts", "10", "--silent"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    w3 = Web3(Web3.HTTPProvider(url))
    deadline = time.time() + 15
    while time.time() < deadline:
        if w3.is_connected():
            break
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("anvil failed to start")
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def env(anvil, tmp_path):
    """Fresh deployment + API client per test."""
    settings = Settings(
        rpc_url=anvil,
        chain_id=31337,
        deployments_path=tmp_path / "deployments.json",
    )
    signer_addrs = [Account.from_key(k).address for k in ANVIL_KEYS[:3]]
    deployment = deploy_all(
        settings,
        signer_addresses=signer_addrs,
        threshold=2,
        timelock=2,
        max_failures=2,
        retry_cooldown=1,
    )
    settings.deployments_path.write_text(json.dumps(deployment))

    app = create_app(settings)
    client = TestClient(app)
    return SimpleNamespace(
        client=client,
        app=app,
        settings=settings,
        deploy=deployment,
        signer_addrs=signer_addrs,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def selector(signature: str) -> str:
    return Web3.keccak(text=signature)[:4].hex()


def now_of(client) -> int:
    return client.get("/state").json()["now"]


def counter_data(client, step: int = 1, tag: bytes = b"http") -> str:
    """Encode Counter.increment(step, tag) calldata via the contract ABI.
    The on-chain parameter is bytes32, so pad/truncate the tag exactly."""
    tag32 = tag.ljust(32, b"\x00")[:32]
    svc = client.app.state.service
    counter = svc.chain.contract("Counter", svc.targets["counter"])
    return counter.encode_abi("increment", args=[step, tag32])


def counter_count(client) -> int:
    svc = client.app.state.service
    counter = svc.chain.contract("Counter", svc.targets["counter"])
    return counter.functions.count().call()


def flaky_successes(client) -> int:
    svc = client.app.state.service
    flaky = svc.chain.contract("FlakyTarget", svc.targets["flaky"])
    return flaky.functions.successes().call()


def propose_counter(client, step=1, indices=(2, 0), deadline=None, data=None):
    """Propose a Counter.increment op; returns (response json, data, deadline)."""
    if deadline is None:
        deadline = now_of(client) + 3600
    if data is None:
        data = counter_data(client, step)
    resp = client.post("/operations/propose", json={
        "target": client.app.state.service.targets["counter"],
        "data": data,
        "deadline": deadline,
        "signer_indices": list(indices),
    })
    return resp.json(), data, deadline
