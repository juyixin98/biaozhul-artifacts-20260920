"""Shared pytest fixtures.

* ``anvil``  (session): boots one ``anvil --no-mining --silent`` on an
  ephemeral port and terminates it at session end.
* ``chain``  (function): per-test genesis snapshot revert, so every test starts
  from a fresh chain (empty mempool included) while reusing the same process.
* ``deployed`` (function): deploys a fresh Vault contract and returns a
  ``Chain`` helper bundling w3/sender/vault/indexer/store.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from app import onchain
from app.indexer import Indexer
from app.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
ANVIL = os.environ.get("ANVIL_BIN", str(Path.home() / ".foundry" / "bin" / "anvil"))
# First Anvil test key: key[0] is the deployer, keys[1]/[2] are the users.
DEPLOY_KEY = onchain.ANVIL_TEST_KEYS[0]
USER1_KEY = onchain.ANVIL_TEST_KEYS[1]
USER2_KEY = onchain.ANVIL_TEST_KEYS[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_contracts_built() -> None:
    """Run `forge build` once if the Vault artifact is missing (fresh clone)."""
    artifact = ROOT / "out" / "Vault.sol" / "Vault.json"
    if artifact.exists():
        return
    forge = os.environ.get("FORGE_BIN", str(Path.home() / ".foundry" / "bin" / "forge"))
    if not Path(forge).exists():
        forge = "forge"  # fall back to PATH
    subprocess.run([forge, "build"], cwd=ROOT, check=True)


@pytest.fixture(scope="session", autouse=True)
def _contracts():
    _ensure_contracts_built()


@pytest.fixture(scope="session")
def anvil():
    port = _free_port()
    cmd = [
        ANVIL,
        "--no-mining",
        "--silent",
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
        "--config-out",
        str(ROOT / "tests" / ".anvil-config.json"),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    rpc = f"http://127.0.0.1:{port}"
    # Wait for the RPC port.
    for _ in range(100):
        try:
            w3 = onchain.make_w3(rpc)
            w3.eth.block_number
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("anvil did not become ready")
    logging.info("anvil ready on %s (pid=%d)", rpc, proc.pid)
    yield rpc
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def chain(anvil):
    w3 = onchain.make_w3(anvil)
    # Fresh-chain isolation for every test.
    gen = onchain.snapshot(w3)
    yield w3
    onchain.revert(w3, gen)


@dataclass
class Deployed:
    w3: object
    sender: onchain.TxSender
    vault: object
    indexer: Indexer
    store: Storage
    deploy_key: str
    user1_key: str
    user2_key: str

    @property
    def user1(self) -> str:
        return onchain.address_for(self.user1_key)

    @property
    def user2(self) -> str:
        return onchain.address_for(self.user2_key)

    def deposit(self, key: str, wei: int) -> str:
        return self.sender.vault_call(key, self.vault, "deposit", value=wei)

    def withdraw(self, key: str, wei: int) -> str:
        return self.sender.vault_call(key, self.vault, "withdraw", wei)


def _make_deployed(w3, confirmation_depth: int = 0) -> Deployed:
    sender = onchain.TxSender(w3)
    addr = onchain.deploy_vault(w3, sender, DEPLOY_KEY)
    vault = onchain.vault_at(w3, addr)
    store = Storage(":memory:")
    indexer = Indexer(
        w3,
        store,
        vault_address=addr,
        start_block=0,
        confirmation_depth=confirmation_depth,
    )
    return Deployed(
        w3=w3,
        sender=sender,
        vault=vault,
        indexer=indexer,
        store=store,
        deploy_key=DEPLOY_KEY,
        user1_key=USER1_KEY,
        user2_key=USER2_KEY,
    )


@pytest.fixture()
def deployed(chain):
    return _make_deployed(chain, confirmation_depth=0)


@pytest.fixture()
def deployed_confirmed(chain):
    return _make_deployed(chain, confirmation_depth=1)
