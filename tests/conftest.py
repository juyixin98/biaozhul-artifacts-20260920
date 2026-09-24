"""Shared pytest fixtures: ephemeral Anvil node, deployed contract, HTTP client."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Ensure ambient env vars never point the app away from the test chain.
os.environ.pop("CONTRACT_ADDRESS", None)

from app.config import ANVIL_TEST_PRIVATE_KEYS, Settings  # noqa: E402
from app.contract import CheckpointContract, deploy, load_abi  # noqa: E402
from app.main import State, app, get_state  # noqa: E402

ANVIL_BIN = str(Path.home() / ".foundry" / "bin" / "anvil")
ARTIFACT = ROOT / "out" / "Checkpoints.sol" / "Checkpoints.json"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bytecode() -> str:
    return json.loads(ARTIFACT.read_text())["bytecode"]["object"]


@pytest.fixture(scope="session")
def anvil():
    """A fresh, ephemeral Anvil node for the whole test session."""
    port = _free_port()
    rpc_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            ANVIL_BIN,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--chain-id", "31337",
            "--accounts", "10",
            "--silent",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    deadline = time.time() + 20
    while time.time() < deadline:
        if w3.is_connected():
            break
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("Anvil failed to start within 20s")

    yield rpc_url, w3

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def w3(anvil):
    return anvil[1]


@pytest.fixture()
def rpc_url(anvil):
    return anvil[0]


@pytest.fixture()
def checkpoint(w3) -> CheckpointContract:
    """A freshly deployed Checkpoints contract per test."""
    return deploy(w3, ANVIL_TEST_PRIVATE_KEYS[0], _bytecode())


@pytest.fixture()
def client(rpc_url, checkpoint) -> TestClient:
    """HTTP client wired to the freshly deployed contract (no env needed)."""
    state = State(
        Settings(
            rpc_url=rpc_url,
            chain_id=31337,
            private_key=ANVIL_TEST_PRIVATE_KEYS[0],
            contract_address=checkpoint.address,
        )
    )
    app.state.prebuilt_state = state
    app.dependency_overrides[get_state] = lambda: state
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    app.state.prebuilt_state = None


def mine_empty_blocks(w3: Web3, n: int) -> None:
    w3.provider.make_request("anvil_mine", [hex(n)])


def batch_set_in_one_block(w3: Web3, contract_address: str, values: list[int]):
    """Send all setValue txs from the deployer and force them into ONE block.

    Returns (block_number, [receipts]). Relies on Anvil mining controls.
    """
    key = ANVIL_TEST_PRIVATE_KEYS[0]
    acct = w3.eth.account.from_key(key)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(contract_address), abi=load_abi()
    )

    # Make sure the batch starts in a brand-new block.
    w3.provider.make_request("evm_mine", [])
    w3.provider.make_request("anvil_setAutomine", [False])
    try:
        nonce = w3.eth.get_transaction_count(acct.address)
        gas_price = w3.eth.gas_price
        chain_id = int(w3.eth.chain_id)
        raw_txs = []
        for i, value in enumerate(values):
            tx = contract.functions.setValue(int(value)).build_transaction(
                {
                    "from": acct.address,
                    "nonce": nonce + i,
                    "gas": 200_000,
                    "gasPrice": gas_price,
                    "chainId": chain_id,
                }
            )
            signed = w3.eth.account.sign_transaction(tx, key)
            raw_txs.append(signed.raw_transaction)

        hashes = [w3.eth.send_raw_transaction(raw) for raw in raw_txs]
        # One single block for the whole mempool.
        w3.provider.make_request("evm_mine", [])
        receipts = [w3.eth.wait_for_transaction_receipt(h) for h in hashes]
    finally:
        w3.provider.make_request("anvil_setAutomine", [True])

    blocks = {int(r["blockNumber"]) for r in receipts}
    assert len(blocks) == 1, f"batch must land in one block, got {blocks}"
    assert all(int(r["status"]) == 1 for r in receipts), "all txs succeed"
    return blocks.pop(), receipts
