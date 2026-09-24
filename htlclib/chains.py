"""Anvil process management for the two local test chains.

Chain A: http://127.0.0.1:8545  chain id 31337
Chain B: http://127.0.0.1:8546  chain id 31338

Only local Anvil and its well-known test keys are used. Nothing here ever
touches a public network.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from web3 import Web3

# First two Anvil default test accounts (index 0 and 1). Public keys, no funds
# at risk; this project only ever talks to ephemeral local Anvil nodes.
ALICE_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
BOB_PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

CHAIN_A_RPC = os.environ.get("CHAIN_A_RPC", "http://127.0.0.1:8545")
CHAIN_B_RPC = os.environ.get("CHAIN_B_RPC", "http://127.0.0.1:8546")
CHAIN_A_ID = int(os.environ.get("CHAIN_A_ID", "31337"))
CHAIN_B_ID = int(os.environ.get("CHAIN_B_ID", "31338"))

# Timeout convention (README "时间条件约定"):
#   chain B leg expires DELTA seconds BEFORE chain A leg.
#   T_A = lock time + TTL_A,  T_B = T_A - DELTA.
# Rationale: the party that reveals the preimage (Alice, refund-side on A)
# must have DELTA seconds after B's deadline to also claim on A before A's
# deadline. DELTA must exceed the worst-case halt/confirmation gap on a chain;
# a halt longer than DELTA breaks this safety margin (see coordinator risk demo).
DELTA_SECONDS = int(os.environ.get("HTLC_DELTA_SECONDS", "20"))
TTL_A_SECONDS = int(os.environ.get("HTLC_TTL_A_SECONDS", "60"))


@dataclass
class AnvilChain:
    name: str
    rpc: str
    chain_id: int
    port: int
    process: Optional[subprocess.Popen] = None
    logfile: Optional[object] = None

    @property
    def w3(self) -> Web3:
        provider = Web3.HTTPProvider(self.rpc, request_kwargs={"timeout": 3})
        return Web3(provider)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.logfile is not None:
            self.logfile.close()
            self.logfile = None

    # --- time / mining control used by the risk-boundary demos ---

    def evm_set_interval(self, seconds: int) -> None:
        self._rpc("evm_setIntervalMining", [seconds])

    def evm_mine(self) -> None:
        self._rpc("evm_mine", [])

    def evm_warp(self, timestamp: int) -> None:
        """Set next block's timestamp and mine it."""
        self._rpc("evm_setNextBlockTimestamp", [int(timestamp)])
        self._rpc("evm_mine", [])

    def pause_mining(self) -> None:
        """Simulate a chain halt: no new blocks, pending txs stay pending."""
        self._rpc("evm_setAutomine", [False])
        self._rpc("evm_setIntervalMining", [0])

    def resume_mining(self, interval: int = 0) -> None:
        self._rpc("evm_setAutomine", [True])
        if interval:
            self._rpc("evm_setIntervalMining", [interval])

    def _rpc(self, method: str, params: list) -> None:
        self.w3.provider.make_request(method, params)


def _anvil_bin() -> str:
    found = shutil.which("anvil")
    if found:
        return found
    home = os.path.expanduser("~/.foundry/bin/anvil")
    if os.path.exists(home):
        return home
    raise RuntimeError("anvil not found on PATH or in ~/.foundry/bin")


def spawn_chain(name: str, rpc: str, chain_id: int, log_dir: Path) -> AnvilChain:
    port = int(rpc.rsplit(":", 1)[1])
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = open(log_dir / f"anvil-{name}.log", "w")
    cmd = [
        _anvil_bin(),
        "--port",
        str(port),
        "--chain-id",
        str(chain_id),
        "--silent",
        # Default Anvil behaviour: instant automining. Demos pause mining
        # explicitly via evm_setAutomine(false) to simulate chain halts.
    ]
    proc = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT)
    chain = AnvilChain(name=name, rpc=rpc, chain_id=chain_id, port=port,
                       process=proc, logfile=logfile)
    _wait_ready(chain)
    return chain


def _wait_ready(chain: AnvilChain, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        if chain.process is not None and chain.process.poll() is not None:
            raise RuntimeError(f"anvil {chain.name} exited early; see its log")
        try:
            if chain.w3.is_connected():
                return
        except Exception as exc:  # connection refused until the socket is up
            last_err = exc
        time.sleep(0.2)
    raise RuntimeError(f"anvil {chain.name} not ready at {chain.rpc}: {last_err}")


def spawn_both(log_dir: Path | str = "logs",
               rpc_a: str | None = None,
               rpc_b: str | None = None,
               id_a: int | None = None,
               id_b: int | None = None) -> tuple[AnvilChain, AnvilChain]:
    log_path = Path(log_dir)
    chain_a = spawn_chain("A", rpc_a or CHAIN_A_RPC, id_a or CHAIN_A_ID, log_path)
    chain_b = spawn_chain("B", rpc_b or CHAIN_B_RPC, id_b or CHAIN_B_ID, log_path)
    return chain_a, chain_b


def connect(rpc: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 3}))
    if not w3.is_connected():
        raise ConnectionError(f"cannot reach local chain at {rpc}")
    return w3


def rpc_chain_id(rpc: str) -> int | None:
    """Best-effort chain id for health checks; None when unreachable."""
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 2}))
        if not w3.is_connected():
            return None
        return w3.eth.chain_id
    except Exception:
        return None


def dump_deployment(path: Path | str, chain_a_addr: str, chain_b_addr: str) -> None:
    Path(path).write_text(json.dumps(
        {"chain_a": {"rpc": CHAIN_A_RPC, "chain_id": CHAIN_A_ID, "address": chain_a_addr},
         "chain_b": {"rpc": CHAIN_B_RPC, "chain_id": CHAIN_B_ID, "address": chain_b_addr}},
        indent=2) + "\n")
