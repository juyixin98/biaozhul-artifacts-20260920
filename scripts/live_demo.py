"""End-to-end live demo on local Anvil nodes (acyclic fork DAG).

    BASE (blocks 0..2: genesis, deploy, deposit 1000)
      ├─ X: block 3 deposit 700
      └─ Y: block 3 deposit 5555
    OBSERVER (what the indexer polls): reset BASE@2 -> X@3 -> Y@3

The script drives an indexer HTTP server in-process and prints the state
after each step, including the automatic reorg rollback X -> Y.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.abi import LEDGER_ABI  # noqa: E402
from app.config import DEFAULT_TEST_KEY  # noqa: E402
from app.main import build_app  # noqa: E402
from tests.conftest import AnvilNode, build_tx, free_port, send_mined  # noqa: E402


def line(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main() -> None:
    nodes: list[AnvilNode] = []
    base = AnvilNode(free_port())
    nodes.append(base)
    try:
        w3 = base.w3
        acct = w3.eth.account.from_key(DEFAULT_TEST_KEY)
        from tests.test_reorg import deploy_on

        address, ledger, _ = deploy_on(base)
        send_mined(w3, acct, build_tx(w3, acct, ledger, "deposit", [1000, 101]), base)
        line(f"BASE ready head={w3.eth.block_number} contract={address}")

        # branch X and Y both fork BASE@2 and never reset
        node_x = AnvilNode(free_port(), fork_url=base.rpc, fork_block=2)
        nodes.append(node_x)
        lx = node_x.w3.eth.contract(address=address, abi=LEDGER_ABI)
        ax = node_x.w3.eth.account.from_key(acct.key)
        send_mined(node_x.w3, ax, build_tx(node_x.w3, ax, lx, "deposit", [700, 201]), node_x)
        h_x3 = "0x" + node_x.w3.eth.get_block(3).hash.hex()

        node_y = AnvilNode(free_port(), fork_url=base.rpc, fork_block=2)
        nodes.append(node_y)
        ly = node_y.w3.eth.contract(address=address, abi=LEDGER_ABI)
        ay = node_y.w3.eth.account.from_key(acct.key)
        send_mined(node_y.w3, ay, build_tx(node_y.w3, ay, ly, "deposit", [5555, 301]), node_y)
        h_y3 = "0x" + node_y.w3.eth.get_block(3).hash.hex()
        print(f"X block3={h_x3[:18]}...  Y block3={h_y3[:18]}...", flush=True)

        # observer is the ONLY reset node; indexer polls it. Reset BEFORE the
        # app starts so the poller never records the observer's standalone
        # genesis (which differs from the forked chain's genesis).
        observer = AnvilNode(free_port())
        nodes.append(observer)
        observer.reset(base.rpc, fork_block=2)

        db = Path(tempfile.mkdtemp()) / "live.db"
        app = build_app(
            rpc_url=observer.rpc,
            contract_address=address,
            db_path=str(db),
            start_block=0,
            confirmations=1,
            poll_interval=0.3,
            auto_start=False,
        )
        with TestClient(app) as http:
            r = http.post("/sync").json()
            line("observer -> BASE@2 (initial sync)")
            print("ingested blocks:", len(r["ingested_block_hashes"]),
                  "events:", r["ingested_events"], flush=True)
            print("state:", http.get("/state").json()["balances"], flush=True)

            observer.reset(node_x.rpc, fork_block=3)
            r = http.post("/sync").json()
            line("observer -> X@3 (expect 1000 + 700 = 1700)")
            print("reorg:", r["reorg"], "ingested events:", r["ingested_events"], flush=True)
            print("state:", http.get("/state").json()["balances"], flush=True)

            observer.reset(node_y.rpc, fork_block=3)
            line("REORG observer -> Y@3 (expect rollback of X: 1000 + 5555 = 6555)")
            sync = http.post("/sync").json()
            print(
                "reorg:", sync["reorg"],
                "fork_point:", sync["fork_point"],
                "detached:", sync["detached_block_hashes"],
                flush=True,
            )
            print("state:", http.get("/state").json()["balances"], flush=True)
            orphans = http.get("/blocks/orphans").json()
            print("orphan blocks:", [(b["number"], b["hash"][:18]) for b in orphans], flush=True)
            tags = [e["tag"] for e in http.get("/events").json()]
            print("canonical event tags:", tags, flush=True)
            assert "201" not in tags and "301" in tags
            print("\nLIVE DEMO OK", flush=True)
    finally:
        for n in reversed(nodes):
            n.stop()


if __name__ == "__main__":
    main()
