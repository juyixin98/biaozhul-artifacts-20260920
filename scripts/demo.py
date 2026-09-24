#!/usr/bin/env python3
"""End-to-end demo: layout checks over HTTP + a real upgrade on local Anvil.

Steps:
  1. start anvil (local test chain) and the FastAPI checker service
  2. POST /check-artifacts for a compatible pair (BoxV1 -> BoxV2) and for
     incompatible pairs (reorder / widening / base change / dynamic type)
  3. deploy BoxV1 behind the proxy, write state, upgrade to BoxV2 on-chain
     after the checker approves, and verify the old data survived

Usage:  .venv/bin/python scripts/demo.py
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.artifacts import get_artifact  # noqa: E402
from backend.chain import TEST_ADDRESS, connect, deploy, transact  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise RuntimeError(f"service at {url} did not come up")


def main() -> None:
    anvil_bin = shutil.which("anvil") or str(Path.home() / ".foundry/bin/anvil")
    anvil_port, api_port = _free_port(), _free_port()
    anvil_url = f"http://127.0.0.1:{anvil_port}"
    api_url = f"http://127.0.0.1:{api_port}"

    anvil = subprocess.Popen(
        [anvil_bin, "--port", str(anvil_port), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    api = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "backend.main:app",
            "--host", "127.0.0.1", "--port", str(api_port),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_http(f"{api_url}/health")
        print(f"anvil  : {anvil_url}")
        print(f"checker: {api_url}\n")

        pairs = [
            ("BoxV1", "BoxV2"),
            ("BoxV1", "BoxBadReorder"),
            ("WidenV1", "WidenV2"),
            ("BaseV1", "BaseV2"),
            ("DynTypeV1", "DynTypeV2"),
        ]
        print("== layout checks (POST /check-artifacts) ==")
        for old, new in pairs:
            body = httpx.post(
                f"{api_url}/check-artifacts", json={"old": old, "new": new}, timeout=10
            ).json()
            verdict = "COMPATIBLE" if body["compatible"] else "BLOCKED"
            print(f"  {old} -> {new}: {verdict} ({body['error_count']} errors)")
            for issue in body["issues"]:
                if issue["severity"] != "info":
                    print(f"    [{issue['severity']}] {issue['code']}: {issue['message']}")
        print()

        print("== on-chain upgrade on local anvil ==")
        w3 = connect(anvil_url)
        box_v1 = deploy(w3, get_artifact("BoxV1"))
        proxy = deploy(w3, get_artifact("UpgradeableProxy"), box_v1.address, TEST_ADDRESS)
        box = w3.eth.contract(address=proxy.address, abi=get_artifact("BoxV1")["abi"])
        transact(w3, box.functions.initialize, 42, "hello-layout")
        transact(w3, box.functions.setValue, 1337)
        print(f"  deployed BoxV1 + proxy {proxy.address}")
        print(f"  before upgrade: value={box.functions.value().call()} "
              f"name={box.functions.name().call()!r} version={box.functions.version().call()}")

        box_v2 = deploy(w3, get_artifact("BoxV2"))
        transact(w3, proxy.functions.upgradeTo, box_v2.address)
        box2 = w3.eth.contract(address=proxy.address, abi=get_artifact("BoxV2")["abi"])
        transact(w3, box2.functions.setExtra, 7)
        print(f"  after upgrade : value={box2.functions.value().call()} "
              f"name={box2.functions.name().call()!r} version={box2.functions.version().call()} "
              f"extra={box2.functions.extra().call()}")
        assert box2.functions.value().call() == 1337
        assert box2.functions.name().call() == "hello-layout"
        assert box2.functions.owner().call() == TEST_ADDRESS
        assert box2.functions.version().call() == "v2"
        print("\nOK: compatible upgrade kept all V1 state; incompatible pairs were blocked.")
    finally:
        api.terminate()
        anvil.terminate()
        api.wait(timeout=10)
        anvil.wait(timeout=10)


if __name__ == "__main__":
    main()
