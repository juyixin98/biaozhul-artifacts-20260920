"""Session fixture: build contracts, start a local anvil, deploy, serve the API.

Environment variables are set BEFORE importing app modules (app.config reads
them at import time).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

ANVIL_PORT = 18545
DEPLOYMENT_PATH = os.path.join(tempfile.gettempdir(), "fee_settlement_test_deployment.json")

os.environ["RPC_URL"] = f"http://127.0.0.1:{ANVIL_PORT}"
os.environ["DEPLOYMENT_FILE"] = DEPLOYMENT_PATH
os.environ["FORGE_OUT"] = os.path.join(REPO_ROOT, "out")
os.environ.pop("CONTRACT_ADDRESS", None)

import pytest


def _find_tool(name):
    path = shutil.which(name)
    if path:
        return path
    candidate = os.path.expanduser(f"~/.foundry/bin/{name}")
    if os.path.exists(candidate):
        return candidate
    raise RuntimeError(f"{name} not found in PATH or ~/.foundry/bin")


@pytest.fixture(scope="session")
def chain_env():
    # 1. compile contracts
    forge = _find_tool("forge")
    subprocess.run([forge, "build"], cwd=REPO_ROOT, check=True, capture_output=True)

    # 2. start anvil
    anvil = _find_tool("anvil")
    proc = subprocess.Popen(
        [anvil, "--port", str(ANVIL_PORT), "--gas-limit", "300000000", "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        from app import chain

        # 3. wait for the chain
        w3 = None
        for _ in range(100):
            try:
                w3 = chain.get_web3()
                break
            except Exception:
                time.sleep(0.1)
        if w3 is None:
            raise RuntimeError("anvil did not start")

        # 4. deploy
        address = chain.deploy_contract(w3)
        with open(DEPLOYMENT_PATH, "w") as f:
            json.dump({"address": address, "chain_id": w3.eth.chain_id}, f)

        # 5. API client
        from fastapi.testclient import TestClient
        from app.main import app

        yield {
            "w3": w3,
            "contract": chain.get_contract(w3),
            "client": TestClient(app),
            "address": address,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


_account_counter = [0]


@pytest.fixture()
def fresh_account():
    """Factory returning unique, checksummed, never-used account addresses."""
    from web3 import Web3

    def _next():
        _account_counter[0] += 1
        return Web3.to_checksum_address(f"0x{0xF0000000 + _account_counter[0]:040x}")

    return _next
