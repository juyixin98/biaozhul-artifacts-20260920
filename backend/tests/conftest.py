"""pytest 夹具：自包含地管理本机 anvil、部署合约、装配 web3 句柄。

- 使用独立测试端口（默认 8555），避免与开发用 anvil(8545) 冲突；
- 若该端口已有可用 anvil 则复用，否则本夹具启动一个并在会话结束时关闭；
- 通过 monkeypatch 把应用层 config.RPC_URL 指向测试端口，
  HTTP 接口测试因此也打到夹具管理的链上。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

import pytest
from web3 import Web3

from backend.app import chain as chainlib
from backend.app import config as app_config
from backend.app.config import ANVIL_TEST_PRIVATE_KEYS, FORGE_OUT

ANVIL_PORT = int(os.environ.get("ANVIL_TEST_PORT", "8555"))
ANVIL_RPC = f"http://127.0.0.1:{ANVIL_PORT}"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def anvil():
    """会话级：确保测试端口上有一个可用 anvil，返回其 RPC URL。"""
    if not FORGE_OUT.exists():
        pytest.fail("未找到 forge 产物，请先运行 `forge build`")

    anvil_bin = shutil.which("anvil") or os.path.expanduser("~/.foundry/bin/anvil")
    spawned = None
    if not _port_open(ANVIL_PORT):
        spawned = subprocess.Popen(
            [anvil_bin, "--port", str(ANVIL_PORT), "--silent", "--gas-limit", "30000000"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if _port_open(ANVIL_PORT):
                break
            time.sleep(0.1)
        else:
            spawned.kill()
            pytest.fail("anvil 启动超时")

    w3 = Web3(Web3.HTTPProvider(ANVIL_RPC))
    for _ in range(60):
        try:
            if w3.is_connected():
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
    else:
        if spawned is not None:
            spawned.kill()
        pytest.fail("anvil 已启动但 RPC 不可用")

    yield ANVIL_RPC

    if spawned is not None:
        spawned.terminate()
        try:
            spawned.wait(timeout=5)
        except subprocess.TimeoutExpired:
            spawned.kill()


@pytest.fixture(autouse=True)
def _point_app_rpc(anvil, monkeypatch):
    """让 FastAPI 应用层（main.py -> chainlib.connect()）使用测试端口。"""
    monkeypatch.setattr(app_config, "RPC_URL", anvil)


@pytest.fixture()
def bundle(anvil):
    """每个测试重新部署一套合约，保证状态干净。"""
    chainlib.deploy_contracts(anvil, ANVIL_TEST_PRIVATE_KEYS[0])
    return chainlib.load_bundle(anvil)


@pytest.fixture()
def accounts(bundle):
    w3 = bundle.w3
    return [w3.eth.account.from_key(pk) for pk in ANVIL_TEST_PRIVATE_KEYS[:3]]
