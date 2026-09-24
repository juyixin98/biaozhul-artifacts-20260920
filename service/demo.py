"""End-to-end HTTP demo.

Boots its own Anvil node, deploys fresh contracts, starts the FastAPI server
and drives the whole acceptance flow over HTTP:

  1. unordered signatures + timelock wait + exactly-once execution
  2. failed target call -> retry rule -> eventual success
  3. signer-set change -> in-flight op invalidated, new signers govern

Usage:
    python -m service.demo
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from eth_account import Account
from web3 import Web3

from service.chain import ChainClient
from service.config import ANVIL_KEYS, ROOT, Settings
from service.deploy import deploy_all
from service.signing import sign_op, sign_set_signers

ROOT_BIN = Path.home() / ".foundry" / "bin"


def _bin(name: str) -> str:
    return shutil.which(name) or str(ROOT_BIN / name)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(client: httpx.Client, path: str, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if client.get(path).status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.3)
    raise RuntimeError(f"server never came up at {path}")


class Reporter:
    def __init__(self) -> None:
        self.steps: list[tuple[bool, str]] = []

    def step(self, title: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        self.steps.append((ok, title))
        print(f"  [{mark}] {title}{(' - ' + detail) if detail else ''}")

    def summary(self) -> int:
        passed = sum(1 for ok, _ in self.steps if ok)
        print(f"\n{passed}/{len(self.steps)} checks passed")
        return 0 if passed == len(self.steps) else 1


def main() -> int:
    rpc_port = _free_port()
    http_port = _free_port()
    rpc_url = f"http://127.0.0.1:{rpc_port}"
    base = f"http://127.0.0.1:{http_port}"
    deployments_path = Path("/tmp/multisig-demo-deployments.json")

    anvil = subprocess.Popen(
        [_bin("anvil"), "--port", str(rpc_port), "--chain-id", "31337",
         "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    server = None
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        for _ in range(50):
            if w3.is_connected():
                break
            time.sleep(0.2)

        settings = Settings(
            rpc_url=rpc_url, chain_id=31337, deployments_path=deployments_path,
        )
        print("deploying contracts ...")
        signers = [Account.from_key(k).address for k in ANVIL_KEYS[:3]]
        deployment = deploy_all(settings, signers, 2, 2, 2, 1)
        deployments_path.write_text(json.dumps(deployment))

        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "service.app:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(http_port)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**__import__("os").environ,
                 "RPC_URL": rpc_url,
                 "DEPLOYMENTS_PATH": str(deployments_path),
                 "CHAIN_ID": "31337"},
        )
        client = httpx.Client(base_url=base, timeout=30)
        _wait_http(client, "/health")

        rpt = Reporter()
        print("\n1) health & state")
        state = client.get("/state").json()
        rpt.step("connected, 2-of-3 signers, nonce 0",
                 state["threshold"] == 2 and state["nonce"] == 0)

        chain = ChainClient(rpc_url, 31337)
        counter_addr = deployment["targets"]["counter"]
        flaky_addr = deployment["targets"]["flaky"]
        counter = chain.contract("Counter", counter_addr)
        flaky = chain.contract("FlakyTarget", flaky_addr)

        def data_counter(step: int, tag: bytes) -> str:
            return counter.encode_abi("increment",
                                      args=[step, tag.ljust(32, b"\x00")[:32]])

        def data_flaky() -> str:
            return flaky.encode_abi("run", args=[])

        def deadline() -> int:
            return client.get("/state").json()["now"] + 3600

        print("\n2) unordered signatures -> timelock -> exactly-once")
        d1 = data_counter(7, b"once")
        dl1 = deadline()
        r = client.post("/operations/propose", json={
            "target": counter_addr, "data": d1, "deadline": dl1,
            "signer_indices": [2, 0],  # deliberately out of order
        }).json()
        rpt.step("out-of-order sigs reached Scheduled",
                 r["op"]["state"] == "Scheduled")

        early = client.post("/operations/execute", json={
            "target": counter_addr, "data": d1, "nonce": 0, "deadline": dl1,
        })
        rpt.step("execute before timelock rejected (TimelockActive)",
                 early.status_code == 400
                 and "TimelockActive" in early.json()["detail"]["revert"])

        client.post("/dev/warp", json={"seconds": 2})
        ok = client.post("/operations/execute", json={
            "target": counter_addr, "data": d1, "nonce": 0, "deadline": dl1,
        }).json()
        on_chain_count = counter.functions.count().call()
        rpt.step("execute after timelock ran callback (count=7)",
                 ok["action"] == "executed" and on_chain_count == 7,
                 f"target msg.sender = executor: "
                 f"{counter.functions.lastCaller().call() == deployment['executor']}")

        again = client.post("/operations/execute", json={
            "target": counter_addr, "data": d1, "nonce": 0, "deadline": dl1,
        })
        rpt.step("second execute rejected (AlreadyTerminal)",
                 again.status_code == 400
                 and "AlreadyTerminal" in again.json()["detail"]["revert"]
                 and counter.functions.callCount().call() == 1)

        print("\n3) failing target -> cooldown retry -> success")
        d2 = data_flaky()
        dl2 = deadline()
        p2 = client.post("/operations/propose", json={
            "target": flaky_addr, "data": d2, "deadline": dl2,
            "signer_indices": [0, 1],
        }).json()
        client.post("/dev/warp", json={"seconds": 2})
        f1 = client.post("/operations/execute", json={
            "target": flaky_addr, "data": d2, "nonce": p2["op"]["nonce"],
            "deadline": dl2,
        }).json()
        rpt.step("failed call recorded, op stays Scheduled (failures=1)",
                 f1["action"] == "failed_retryable"
                 and f1["op"]["failures"] == 1
                 and f1["op"]["state"] == "Scheduled")

        soon = client.post("/operations/execute", json={
            "target": flaky_addr, "data": d2, "nonce": p2["op"]["nonce"],
            "deadline": dl2,
        })
        rpt.step("retry inside cooldown rejected (RetryCooldownActive)",
                 soon.status_code == 400
                 and "RetryCooldownActive" in soon.json()["detail"]["revert"])

        client.post("/dev/flaky", json={"failing": False})
        client.post("/dev/warp", json={"seconds": 2})
        f2 = client.post("/operations/execute", json={
            "target": flaky_addr, "data": d2, "nonce": p2["op"]["nonce"],
            "deadline": dl2,
        }).json()
        rpt.step("retry after cooldown succeeds once",
                 f2["action"] == "executed"
                 and flaky.functions.successes().call() == 1)

        print("\n4) signer change invalidates in-flight ops; new set governs")
        # One more op scheduled under the old set (nonce 2)...
        d3 = data_counter(3, b"pre-change")
        dl3 = deadline()
        p3 = client.post("/operations/propose", json={
            "target": counter_addr, "data": d3, "deadline": dl3,
            "signer_indices": [0, 1],
        }).json()
        # ...then a governance op (nonce 3) swaps to {s1,s2,s3}/3.
        new_set = [Account.from_key(ANVIL_KEYS[i]).address for i in (1, 2, 3)]
        dl_gov = deadline()
        gov_nonce = client.get("/state").json()["nonce"]
        gov_sigs = [
            sign_set_signers(ANVIL_KEYS[i], 31337, deployment["executor"],
                             new_set, 3, gov_nonce, dl_gov, 0)
            for i in (0, 1)
        ]
        g = client.post("/admin/signers", json={
            "signers": new_set, "threshold": 3, "deadline": dl_gov,
            "signatures": gov_sigs,
        })
        rpt.step("signer change accepted, config_version -> 1",
                 g.status_code == 200 and g.json()["config_version"] == 1,
                 g.text if g.status_code != 200 else "")

        stale_op = client.get(f"/operations/{p3['op_id']}").json()
        rpt.step("in-flight op marked Invalidated",
                 stale_op["state"] == "Invalidated")

        # Fresh op nonce 4 signed by the NEW set {s1,s2,s3}.
        d4 = data_counter(11, b"new-set")
        dl4 = deadline()
        n4 = client.get("/state").json()["nonce"]
        new_sigs = [
            sign_op(ANVIL_KEYS[i], 31337, deployment["executor"],
                    counter_addr, 0, d4, n4, dl4, 1)
            for i in (1, 2, 3)
        ]
        p4 = client.post("/operations/propose", json={
            "target": counter_addr, "data": d4, "deadline": dl4,
            "signatures": new_sigs,
        }).json()
        client.post("/dev/warp", json={"seconds": 2})
        e4 = client.post("/operations/execute", json={
            "target": counter_addr, "data": d4, "nonce": n4, "deadline": dl4,
        }).json()
        rpt.step("new signer set scheduled and executed an op (count=18)",
                 p4.get("op", {}).get("state") == "Scheduled"
                 and e4["action"] == "executed"
                 and counter.functions.count().call() == 18)

        print(f"\nAPI: {base}  (docs at {base}/docs)")
        return rpt.summary()
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        anvil.terminate()
        try:
            anvil.wait(timeout=5)
        except subprocess.TimeoutExpired:
            anvil.kill()


if __name__ == "__main__":
    raise SystemExit(main())
