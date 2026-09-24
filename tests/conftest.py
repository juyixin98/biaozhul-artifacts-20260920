"""Pytest fixtures: ephemeral local Anvil nodes, a deployed Ledger and
helpers for building branches via anvil_reset forks."""

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

from app.abi import LEDGER_ABI
from app.config import DEFAULT_TEST_KEY
from scripts.deploy import deploy

ANVIL = os.environ.get("ANVIL_BIN") or os.path.expanduser("~/.foundry/bin/anvil")
REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "contracts/out/Ledger.sol/Ledger.json"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for_rpc(w3: Web3, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            w3.eth.block_number
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.1)
    raise RuntimeError(f"anvil RPC never came up: {last}")


class AnvilNode:
    def __init__(self, port: int, fork_url: str | None = None, fork_block: int | None = None):
        self.port = port
        self.rpc = f"http://127.0.0.1:{port}"
        self.w3 = Web3(Web3.HTTPProvider(self.rpc))
        cmd = [
            ANVIL,
            "--port", str(port),
            "--host", "127.0.0.1",
            "--silent",
            "--accounts", "10",
            "--no-mining",  # deterministic blocks; tests call evm_mine
        ]
        if fork_url:
            cmd += ["--fork-url", fork_url]
            if fork_block is not None:
                cmd += ["--fork-block-number", str(fork_block)]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        wait_for_rpc(self.w3)
        self.w3.provider.make_request("evm_setAutomine", [False])
        self.w3.provider.make_request("evm_setIntervalMining", [0])

    def mine(self, n: int = 1) -> None:
        for _ in range(n):
            self.w3.provider.make_request("evm_mine", [])

    def reset(self, fork_url: str, fork_block: int | None = None) -> None:
        """Point this node at another node's chain (reorg primitive)."""
        params = [{"forking": {"jsonRpcUrl": fork_url}}]
        if fork_block is not None:
            params[0]["forking"]["blockNumber"] = fork_block
        res = self.w3.provider.make_request("anvil_reset", params)
        if "error" in res:
            raise RuntimeError(f"anvil_reset failed: {res['error']}")
        wait_for_rpc(self.w3)
        # reset restores default auto-mining; turn it back off
        self.w3.provider.make_request("evm_setAutomine", [False])
        self.w3.provider.make_request("evm_setIntervalMining", [0])

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def _anvil_available() -> bool:
    return shutil.which(ANVIL) is not None and ARTIFACT.exists()


pytestmark = None


@pytest.fixture
def node_a():
    if not _anvil_available():
        pytest.skip("anvil or compiled artifact unavailable")
    n = AnvilNode(free_port())
    try:
        yield n
    finally:
        n.stop()


@pytest.fixture
def node_factory():
    if not _anvil_available():
        pytest.skip("anvil or compiled artifact unavailable")
    nodes: list[AnvilNode] = []

    def make(fork_url: str | None = None, fork_block: int | None = None) -> AnvilNode:
        n = AnvilNode(free_port(), fork_url=fork_url, fork_block=fork_block)
        nodes.append(n)
        return n

    yield make
    for n in nodes:
        n.stop()


@pytest.fixture
def deployed(node_a):
    """(node, Ledger contract, sender account) on a fresh Anvil chain."""
    address = deploy(node_a.rpc, DEFAULT_TEST_KEY)
    contract = node_a.w3.eth.contract(address=address, abi=LEDGER_ABI)
    acct = node_a.w3.eth.account.from_key(DEFAULT_TEST_KEY)
    return node_a, contract, acct


def build_tx(w3: Web3, acct, contract, fn_name: str, args: list):
    """Build an *unsigned* EIP-1559 tx dict for a Ledger call."""
    fn = getattr(contract.functions, fn_name)(*args)
    tx = fn.build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 200_000,
            "maxFeePerGas": w3.to_wei(20, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
            "chainId": w3.eth.chain_id,
        }
    )
    return tx


def send_mined(w3: Web3, acct, tx: dict, node: AnvilNode):
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    node.mine()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt["status"] == 1
    return receipt


def signed_raw(acct, tx: dict) -> bytes:
    return acct.sign_transaction(tx).raw_transaction


def send_raw_mined(w3: Web3, raw: bytes, node: AnvilNode):
    tx_hash = w3.eth.send_raw_transaction(raw)
    node.mine()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt["status"] == 1
    return receipt
