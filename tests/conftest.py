"""pytest 夹具：启动本地 Anvil、编译并部署合约、构造 FastAPI 测试客户端。

所有链状态都在本机 Anvil 内存链上；每个测试使用独立部署与临时部署文件。
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

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
PYTHON = ROOT / ".venv" / "bin" / "python"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_rpc(w3: Web3, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if w3.is_connected():
                _ = w3.eth.block_number
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("anvil 启动超时")


@pytest.fixture(scope="session")
def forge_build():
    env = os.environ.copy()
    env["PATH"] = f"{FOUNDRY_BIN}:{env['PATH']}"
    r = subprocess.run(["forge", "build"], cwd=CONTRACTS, capture_output=True,
                       text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"forge build 失败:\n{r.stdout}\n{r.stderr}")
    return True


@pytest.fixture()
def chain(forge_build, tmp_path):
    """启动 anvil 并部署合约，返回 {rpc, w3, dep_file, deployment}。"""
    port = _free_port()
    rpc = f"http://127.0.0.1:{port}"
    dep_file = tmp_path / "deployments.json"

    env = os.environ.copy()
    env["PATH"] = f"{FOUNDRY_BIN}:{env['PATH']}"
    anvil = subprocess.Popen(
        ["anvil", "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
    try:
        _wait_rpc(w3)
        dep_out = subprocess.run(
            [str(PYTHON), str(ROOT / "scripts" / "deploy.py"),
             "--rpc", rpc, "--delay", "2", "--cooldown", "2",
             "--signer-count", "4", "--threshold", "2",
             "--out", str(dep_file)],
            capture_output=True, text=True, cwd=ROOT,
        )
        if dep_out.returncode != 0:
            raise RuntimeError(f"部署失败:\n{dep_out.stdout}\n{dep_out.stderr}")
        yield {
            "rpc": rpc,
            "w3": w3,
            "dep_file": dep_file,
            "deployment": json.loads(dep_file.read_text()),
        }
    finally:
        anvil.terminate()
        try:
            anvil.wait(timeout=5)
        except subprocess.TimeoutExpired:
            anvil.kill()


@pytest.fixture()
def client(chain):
    from fastapi.testclient import TestClient
    from app.chain import WalletService
    from app.main import create_app, store

    svc = WalletService.from_deployment(chain["dep_file"])
    application = create_app(svc)
    store.ops.clear()
    with TestClient(application) as c:
        yield {"http": c, "svc": svc, "store": store, "chain": chain}


def evm_increase_time(w3: Web3, seconds: int) -> None:
    """anvil 专用：evm_increaseTime + evm_mine。"""
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])
