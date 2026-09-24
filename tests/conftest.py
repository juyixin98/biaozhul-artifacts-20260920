"""Pytest fixtures: spin up a fresh local Anvil chain, build & deploy contracts.

A single chain is shared for the whole test session for speed; each test creates
its own schedule ids so they stay independent.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
PORT = 8546
RPC_URL = f"http://127.0.0.1:{PORT}"
# Anvil deterministic account #0 (deployer/owner) and #1 (beneficiary).
PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
BENEFICIARY_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
BENEFICIARY = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

TOK = 10**18

# Ensure the in-process API/web3 layer talks to the test node (conftest runs in
# the pytest process, whereas env vars below are also passed to subprocesses).
os.environ.setdefault("RPC_URL", RPC_URL)
os.environ.setdefault("PRIVATE_KEY", PRIVATE_KEY)


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"anvil did not open port {port}")


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)


@dataclass
class Env:
    w3: Web3
    token: object
    vesting: object
    owner: object
    beneficiary: object
    addresses: dict

    # -- time travel ---------------------------------------------------------
    def travel(self, timestamp: int) -> None:
        """Move the chain clock to an absolute timestamp, mining one block.

        `evm_setTime` (absolute) is used instead of `evm_setNextBlockTimestamp`
        because the latter is consumed by the very next block and pairing it
        with a separate `evm_mine` RPC can race; setTime guarantees every
        subsequent block timestamp is >= the requested value. Anvil 1.8 takes
        the value in seconds.
        """
        self.w3.provider.make_request("evm_setTime", [int(timestamp)])
        self.w3.provider.make_request("evm_mine", [])

    def now(self) -> int:
        return int(self.w3.eth.get_block("latest")["timestamp"])

    # -- conveniences --------------------------------------------------------
    def send(self, account, func):
        tx = func.build_transaction(
            {
                "from": account.address,
                "nonce": self.w3.eth.get_transaction_count(account.address),
                "gas": int(func.estimate_gas({"from": account.address}) * 1.25),
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.w3.eth.chain_id,
            }
        )
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        h = self.w3.eth.send_raw_transaction(raw)
        rcpt = self.w3.eth.wait_for_transaction_receipt(h)
        assert int(rcpt.status) == 1, f"tx reverted: {h.hex()}"
        return rcpt

    def create(self, total, start, cliff, end, beneficiary=None):
        fn = self.vesting.functions.createSchedule(
            beneficiary or self.beneficiary.address,
            self.token.address,
            total,
            start,
            cliff,
            end,
        )
        rcpt = self.send(self.owner, fn)
        # scheduleId is available via nextScheduleId after creation
        return int(self.vesting.functions.nextScheduleId().call()), rcpt

    def approve_max(self):
        self.send(self.owner, self.token.functions.approve(self.vesting.address, 2**256 - 1))

    def bal(self, who) -> int:
        return int(self.token.functions.balanceOf(who).call())


@pytest.fixture(scope="session")
def env() -> Env:
    env_vars = os.environ.copy()
    env_vars["PATH"] = f"{FOUNDRY_BIN}:{env_vars['PATH']}"
    env_vars["RPC_URL"] = RPC_URL
    env_vars["PRIVATE_KEY"] = PRIVATE_KEY

    anvil = subprocess.Popen(
        [str(FOUNDRY_BIN / "anvil"), "--host", "127.0.0.1", "--port", str(PORT),
         "--chain-id", "31337", "--silent"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(PORT)

        build = _run(["forge", "build"], env_vars)
        if build.returncode != 0:
            raise RuntimeError(f"forge build failed:\n{build.stdout}\n{build.stderr}")

        deploy = _run([sys.executable, "scripts/deploy.py"], env_vars)
        if deploy.returncode != 0:
            raise RuntimeError(f"deploy failed:\n{deploy.stdout}\n{deploy.stderr}")

        from app.chain import get_contract

        addresses = json.loads((ROOT / "deployment" / "addresses.json").read_text())
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        token = get_contract(w3, "SyntheticToken", addresses["token"])
        vesting = get_contract(w3, "TokenVesting", addresses["vesting"])
        owner = w3.eth.account.from_key(PRIVATE_KEY)
        beneficiary = w3.eth.account.from_key(BENEFICIARY_KEY)

        yield Env(w3, token, vesting, owner, beneficiary, addresses)
    finally:
        anvil.terminate()
        try:
            anvil.wait(timeout=10)
        except subprocess.TimeoutExpired:
            anvil.kill()


@pytest.fixture(autouse=True)
def _isolate_chain_state(env: Env):
    """Give every test a fresh post-deploy chain state.

    Snapshots after deployment and reverts after each test, so token balances,
    schedule ids and the clock all reset to the baseline. Block timestamp is
    restored too, so absolute test timestamps remain valid across tests.
    """
    snap = env.w3.provider.make_request("evm_snapshot", [])["result"]
    yield
    env.w3.provider.make_request("evm_revert", [snap])
