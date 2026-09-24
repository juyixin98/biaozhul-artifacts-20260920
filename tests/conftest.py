"""端到端测试：本地 Anvil + forge build + 部署 + FastAPI TestClient。"""
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
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def chain_env(tmp_path_factory):
    """启动 anvil、编译、部署，返回 (settings, client, w3, state)。"""
    port = _free_port()
    rpc_url = f"http://127.0.0.1:{port}"
    env = os.environ | {"PATH": f"{FOUNDRY_BIN}:{os.environ['PATH']}"}

    anvil = subprocess.Popen(
        [str(FOUNDRY_BIN / "anvil"), "--port", str(port), "--silent", "--chain-id", "31337"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # 等节点就绪
        ready = False
        for _ in range(60):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready = True
                    break
            except OSError:
                time.sleep(0.5)
        if not ready:
            raise RuntimeError("anvil 启动超时")

        # forge build
        build = subprocess.run(
            [str(FOUNDRY_BIN / "forge"), "build"], cwd=ROOT, env=env,
            capture_output=True, text=True,
        )
        assert build.returncode == 0, f"forge build 失败:\n{build.stdout}\n{build.stderr}"

        # deploy.py
        dep_env = env | {"BOUNDED_RPC_URL": rpc_url}
        dep = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "deploy.py")], cwd=ROOT, env=dep_env,
            capture_output=True, text=True,
        )
        assert dep.returncode == 0, f"deploy 失败:\n{dep.stdout}\n{dep.stderr}"

        from app.config import Settings
        from app.main import create_app

        settings = Settings.load(ROOT / "deployment.json")
        app = create_app(settings)
        client = TestClient(app)
        with client:  # 触发 lifespan
            yield {
                "settings": settings,
                "client": client,
                "state": app.state.svc,
                "w3": app.state.svc.w3,
                "rpc_url": rpc_url,
                "env": env,
            }
    finally:
        anvil.terminate()
        try:
            anvil.wait(timeout=10)
        except subprocess.TimeoutExpired:
            anvil.kill()


@pytest.fixture
def svc(chain_env):
    return chain_env


def sign_order(client, **overrides):
    body = {
        "signer": "maker",
        "sell_token": "A",
        "buy_token": "B",
        "sell_amount": 100,
        "buy_amount": 100,
        "fee_cap": 0,
        "nonce": 1,
        "expiry": int(time.time()) + 3600,
    }
    body.update(overrides)
    r = client.post("/orders/sign", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def fill(client, signed, sell_fill, fee=0, taker="taker"):
    return client.post(
        "/settlements",
        json={
            "order": signed["order"],
            "signature": signed["signature"],
            "sell_fill_amount": sell_fill,
            "fee_amount": fee,
            "taker": taker,
        },
    )


def balances(svc):
    s = svc["state"]
    return {
        "A": {
            "maker": s.token_a.functions.balanceOf(s.settings.maker).call(),
            "taker": s.token_a.functions.balanceOf(s.settings.taker).call(),
            "fee": s.token_a.functions.balanceOf(s.settings.fee_receiver).call(),
        },
        "B": {
            "maker": s.token_b.functions.balanceOf(s.settings.maker).call(),
            "taker": s.token_b.functions.balanceOf(s.settings.taker).call(),
            "fee": s.token_b.functions.balanceOf(s.settings.fee_receiver).call(),
        },
    }


def assert_conserved(before, after):
    for tok in ("A", "B"):
        assert sum(before[tok].values()) == sum(after[tok].values()), f"{tok} 总量不守恒"
