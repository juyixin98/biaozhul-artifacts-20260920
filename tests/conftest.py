"""端到端测试夹具：启动独立的 anvil 进程，通过 HTTP RPC 跑完整部署与领取流程。

不使用 evm_snapshot：每个测试在新部署的 MerkleClaim 合约上进行，
anvil 本身在所有测试间复用（只启动一次）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from web3 import Web3

from app import chain as chain_mod
from app.allocations import load_allocations
from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
ANVIL = os.path.expanduser("~/.foundry/bin/anvil")
ARTIFACT = ROOT / "out" / "MerkleClaim.sol" / "MerkleClaim.json"

# Anvil 固定测试密钥
KEY0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # deployer/claimer
KEY1 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
ADDR0 = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

# 避免与默认 8545 冲突
PORT = int(os.getenv("TEST_ANVIL_PORT", "8555"))
RPC_URL = f"http://127.0.0.1:{PORT}"


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"anvil did not open port {port}")


@pytest.fixture(scope="session")
def anvil():
    if not Path(ANVIL).exists():
        pytest.skip(f"anvil not found at {ANVIL}")
    if not ARTIFACT.exists():
        pytest.skip("contract artifact missing; run `forge build` first")
    proc = subprocess.Popen(
        [
            ANVIL,
            "--port",
            str(PORT),
            "--chain-id",
            "31337",
            "--silent",
            "--accounts",
            "10",
            "--balance",
            "10000",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(PORT)
        yield RPC_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def w3(anvil):
    w = Web3(Web3.HTTPProvider(anvil, request_kwargs={"timeout": 30}))
    assert w.is_connected()
    return w


@pytest.fixture()
def settings(anvil, tmp_path):
    alloc_src = ROOT / "data" / "allocations.json"
    return Settings(
        rpc_url=anvil,
        contract_address=None,
        deployer_private_key=KEY0,
        claimer_private_key=KEY0,
        allocations_path=str(alloc_src),
        artifact_path=str(ARTIFACT),
    )


@pytest.fixture()
def allocations(settings):
    return load_allocations(settings.allocations_path)


@pytest.fixture()
def deployed(w3, settings, allocations):
    """每个测试部署一份全新的 MerkleClaim，返回 (contract, allocations, total)。"""
    total = sum(a.amount_wei for a in allocations)
    contract, address, root, _ = chain_mod.deploy_claim_contract(
        w3, settings.deployer_private_key, allocations, settings.artifact_path, total
    )
    return contract, allocations, total


@pytest.fixture()
def fastapi_client(anvil, settings):
    """带真实链后端的 FastAPI TestClient（启动时未部署，通过 /deploy 部署）。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        yield client
