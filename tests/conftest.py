"""Pytest fixtures: isolated Anvil node + freshly deployed contracts.

Each test session gets its OWN anvil on an isolated port (8547) so parallel
projects on this machine never collide on 8545. Contracts are rebuilt and
deployed with forge/cast from the local Foundry toolchain.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("VEST_TEST_PORT", "8547"))
RPC_URL = f"http://127.0.0.1:{PORT}"

OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
BENEFICIARY_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


def _foundry_bin_dir() -> str:
    for candidate in (
        os.environ.get("FOUNDRY_DIR", ""),
        str(Path.home() / ".foundry"),
        str(Path.home() / ".foundry-vest"),
    ):
        if candidate and (Path(candidate) / "bin" / "anvil").exists():
            return str(Path(candidate) / "bin")
    return ""


def _wait_for_rpc(url: str, timeout: float = 20.0) -> None:
    w3 = Web3(Web3.HTTPProvider(url))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if w3.is_connected():
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"anvil at {url} did not become ready")


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=180
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({' '.join(cmd)}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc.stdout


@pytest.fixture(scope="session")
def chain() -> dict:
    bindir = _foundry_bin_dir()
    anvil = shutil.which("anvil") or (f"{bindir}/anvil" if bindir else None)
    forge = shutil.which("forge") or (f"{bindir}/forge" if bindir else None)
    if not anvil or not forge:
        pytest.skip("Foundry (anvil/forge) not installed — run foundryup first")

    env = dict(os.environ)
    if bindir:
        env["PATH"] = bindir + ":" + env.get("PATH", "")

    log_file = ROOT / "deploy" / "anvil-test.log"
    log_file.parent.mkdir(exist_ok=True)
    log_handle = log_file.open("w")
    proc = subprocess.Popen(
        [anvil, "--host", "127.0.0.1", "--port", str(PORT), "--chain-id", "31337"],
        cwd=ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_rpc(RPC_URL)

        _run([forge, "build"], ROOT)

        supply = str(10_000_000 * 10**18)
        out = _run(
            [forge, "create", "--broadcast", "--rpc-url", RPC_URL, "--private-key", OWNER_KEY,
             "src/SyntheticToken.sol:SyntheticToken", "--constructor-args", supply],
            ROOT,
        )
        token_addr = [l for l in out.splitlines() if "Deployed to:" in l][0].split()[-1]

        out = _run(
            [forge, "create", "--broadcast", "--rpc-url", RPC_URL, "--private-key", OWNER_KEY,
             "src/LinearTokenVesting.sol:LinearTokenVesting", "--constructor-args",
             token_addr],
            ROOT,
        )
        vesting_addr = [l for l in out.splitlines() if "Deployed to:" in l][0].split()[-1]

        # Point the API config at this session's deployment BEFORE importing it.
        os.environ["RPC_URL"] = RPC_URL
        os.environ["TOKEN_ADDRESS"] = token_addr
        os.environ["VESTING_ADDRESS"] = vesting_addr

        yield {
            "rpc_url": RPC_URL,
            "token": Web3.to_checksum_address(token_addr),
            "vesting": Web3.to_checksum_address(vesting_addr),
            "owner_key": OWNER_KEY,
            "beneficiary_key": BENEFICIARY_KEY,
            "owner": Web3(Web3.HTTPProvider(RPC_URL)).eth.account.from_key(OWNER_KEY).address,
            "beneficiary": Web3(Web3.HTTPProvider(RPC_URL)).eth.account.from_key(BENEFICIARY_KEY).address,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()


def _restore_snapshot(w3: Web3, snapshot_id: str) -> None:
    """Revert state AND reset the chain clock.

    ``evm_revert`` restores accounts/contracts but NOT the timestamp clock
    moved by a prior test's ``evm_setTime``; without resetting it, the next
    test schedules a start in the past/future relative to a skewed clock.
    """
    w3.provider.make_request("evm_revert", [snapshot_id])
    w3.provider.make_request("evm_setTime", [int(time.time())])
    w3.provider.make_request("evm_mine", [])


@pytest.fixture()
def client(chain):
    """Fresh chain state + VestingClient per test (via Anvil snapshot)."""
    from api.client import VestingClient

    w3 = Web3(Web3.HTTPProvider(chain["rpc_url"]))
    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]
    yield VestingClient(
        rpc_url=chain["rpc_url"],
        token_address=chain["token"],
        vesting_address=chain["vesting"],
    )
    _restore_snapshot(w3, snapshot)


@pytest.fixture()
def http_client(chain):
    """FastAPI TestClient; chain state snapshotted and reverted per test."""
    from fastapi.testclient import TestClient

    from api.main import app, get_client

    w3 = Web3(Web3.HTTPProvider(chain["rpc_url"]))
    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]

    vesting_client = __import__("api.client", fromlist=["VestingClient"]).VestingClient(
        rpc_url=chain["rpc_url"],
        token_address=chain["token"],
        vesting_address=chain["vesting"],
    )
    app.dependency_overrides[get_client] = lambda: vesting_client
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    _restore_snapshot(w3, snapshot)
