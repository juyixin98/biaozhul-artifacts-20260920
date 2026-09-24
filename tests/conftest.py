"""pytest 共享夹具：启动 Anvil、forge build、部署合约、启动 FastAPI(uvicorn)。

完整端到端：HTTP 客户端 -> uvicorn -> web3.py -> 本地 Anvil。

前置条件：
  - anvil / forge 已安装（foundryup）
固定测试端口，避免与用户自己开的节点冲突。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ANVIL_PORT = int(os.environ.get("TEST_ANVIL_PORT", "8546"))
API_PORT = int(os.environ.get("TEST_API_PORT", "8011"))
ANVIL_RPC = f"http://127.0.0.1:{ANVIL_PORT}"
API_BASE = f"http://127.0.0.1:{API_PORT}"
ANVIL_KEY0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _wait(url: str, payload: dict, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            r = requests.post(url, json=payload, timeout=1)
            if r.status_code == 200:
                return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.3)
    raise RuntimeError(f"服务 {url} 未在 {timeout}s 内就绪: {last}")


def _wait_http_get(url: str, timeout: float = 20.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(url, timeout=1).status_code == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{url} 未就绪")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture(scope="session")
def anvil():
    if not shutil.which("anvil"):
        pytest.skip("未找到 anvil（执行 foundryup 安装）")
    if not _port_free(ANVIL_PORT):
        pytest.skip(f"测试端口 {ANVIL_PORT} 被占用")
    proc = subprocess.Popen(
        ["anvil", "--port", str(ANVIL_PORT), "--silent", "--chain-id", "31337"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait(ANVIL_RPC, {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
        yield ANVIL_RPC
    finally:
        _terminate(proc)


@pytest.fixture(scope="session")
def forge_build():
    if not shutil.which("forge"):
        pytest.skip("未找到 forge")
    if not (REPO_ROOT / "lib" / "forge-std").exists():
        subprocess.run(["forge", "install", "--no-commit", "foundry-rs/forge-std@v1.9.4"],
                       cwd=REPO_ROOT, check=True)
    r = subprocess.run(["forge", "build"], cwd=REPO_ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"forge build 失败:\n{r.stdout}")


@pytest.fixture(scope="session")
def deployment(anvil, forge_build):
    from deploy.deploy import deploy, load_allocations

    allocs = load_allocations(REPO_ROOT / "examples" / "allocations.json")
    out = REPO_ROOT / "deployments" / "anvil-test.json"
    info = deploy(ANVIL_RPC, ANVIL_KEY0, allocs,
                  token="0x" + "00" * 20, fund_eth=20.0, out_file=out)
    return info


@pytest.fixture(scope="session")
def api_server(deployment):
    if not _port_free(API_PORT):
        pytest.skip(f"API 端口 {API_PORT} 被占用")
    env = os.environ.copy()
    env.update(
        {
            "RPC_URL": ANVIL_RPC,
            "CONTRACT_ADDRESS": deployment["address"],
            "PRIVATE_KEY": ANVIL_KEY0,
            "ALLOCATIONS_FILE": str(REPO_ROOT / "examples" / "allocations.json"),
            "DEPLOY_INFO": str(REPO_ROOT / "deployments" / "anvil-test.json"),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(API_PORT), "--log-level", "warning"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_http_get(f"{API_BASE}/health")
        yield API_BASE
    finally:
        _terminate(proc)


@pytest.fixture()
def http(api_server):
    with httpx.Client(base_url=api_server, timeout=30) as c:
        yield c


@pytest.fixture()
def w3(anvil):
    from web3 import Web3
    return Web3(Web3.HTTPProvider(ANVIL_RPC))
