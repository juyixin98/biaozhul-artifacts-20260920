#!/usr/bin/env python3
"""End-to-end demo: boot Anvil, deploy Vault, index through a TWO-LEVEL reorg.

The script manages its own Anvil process (manual mining), so no external chain
is required. Run from the repo root with the venv active:

    python scripts/demo.py

It prints indexed state on the base chain, then on an uncle branch, triggers a
reorg, verifies the same transaction hash existed under two different block
hashes (and only the canonical one survives), and finally re-opens the
database (simulated restart) to prove persistence. Confirmation-depth gating is
covered separately by tests/test_reorg.py; here depth=0 keeps the narrative
linear.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import onchain  # noqa: E402
from app.indexer import Indexer  # noqa: E402
from app.storage import Storage  # noqa: E402

ANVIL = os.environ.get("ANVIL_BIN", str(Path.home() / ".foundry" / "bin" / "anvil"))
DEPLOY_KEY, USER1_KEY, USER2_KEY = onchain.ANVIL_TEST_KEYS[0:3]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def line(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    port = free_port()
    proc = subprocess.Popen(
        [ANVIL, "--no-mining", "--silent", "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    rpc = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                w3 = onchain.make_w3(rpc)
                w3.eth.block_number
                break
            except Exception:
                time.sleep(0.1)
        else:
            print("anvil failed to start", file=sys.stderr)
            return 1

        workdir = tempfile.mkdtemp(prefix="indexer-demo-")
        db_path = os.path.join(workdir, "indexer.db")
        sender = onchain.TxSender(w3)
        addr = onchain.deploy_vault(w3, sender, DEPLOY_KEY)
        vault = onchain.vault_at(w3, addr)
        store = Storage(db_path)
        idx = Indexer(w3, store, vault_address=addr, start_block=0, confirmation_depth=0)
        user1, user2 = onchain.address_for(USER1_KEY), onchain.address_for(USER2_KEY)
        print(f"chain:    {rpc}")
        print(f"vault:    {addr}")
        print(f"database: {db_path}")
        print(f"user1:    {user1}")

        # base chain: blocks 2,3
        sender.vault_call(USER1_KEY, vault, "deposit", value=100)
        onchain.mine_blocks(w3, 1)
        sender.vault_call(USER1_KEY, vault, "deposit", value=100)
        onchain.mine_blocks(w3, 1)
        idx.poll_once()
        line("base chain indexed (blocks 0..3)")
        print(f"user1 balance: {store.get_balance(user1)} (expect 200)")

        t0 = w3.eth.get_block(3)["timestamp"]
        snap = onchain.snapshot(w3)

        # uncle branch: 4u,5u,6u; block 4u carries a tx that will also appear,
        # byte-identical (same hash), on the canonical chain.
        same_tx = sender.vault_call(USER1_KEY, vault, "deposit", value=50)
        onchain.mine_blocks(w3, 1, timestamps=[t0 + 100])
        sender.vault_call(USER1_KEY, vault, "deposit", value=100)
        onchain.mine_blocks(w3, 1, timestamps=[t0 + 101])
        sender.vault_call(USER2_KEY, vault, "deposit", value=70)
        onchain.mine_blocks(w3, 1, timestamps=[t0 + 102])
        idx.poll_once()
        uncle_4 = onchain.block_info(w3, 4)
        line("UNCLE branch indexed (blocks 4u,5u,6u)")
        print(f"user1: {store.get_balance(user1)} (expect 350)  user2: {store.get_balance(user2)} (expect 70)")
        print(f"same tx {same_tx[:18]}... sits in uncle block hash {uncle_4['hash'][:18]}...")

        # reorg back to block 3 and build canonical 4',5',6'
        onchain.revert(w3, snap)
        sender.reset_nonce_tracking()
        tx2 = sender.vault_call(USER1_KEY, vault, "deposit", value=50)
        assert tx2 == same_tx, "identical tx hash across the fork"
        onchain.mine_blocks(w3, 1, timestamps=[t0 + 200])
        sender.vault_call(USER1_KEY, vault, "deposit", value=200)
        onchain.mine_blocks(w3, 1, timestamps=[t0 + 201])
        sender.vault_call(USER2_KEY, vault, "deposit", value=25)
        onchain.mine_blocks(w3, 1, timestamps=[t0 + 202])
        rep = idx.poll_once()
        canon_4 = onchain.block_info(w3, 4)
        line("REORG -> rolled back uncle blocks, caught up canonical blocks")
        print(f"rolled back: {rep.rolled_back}  fork point: #{rep.fork_point}")
        print(f"re-applied : {rep.applied}")
        print(f"user1: {store.get_balance(user1)} (expect 450)  user2: {store.get_balance(user2)} (expect 25)")
        print(f"deposited total: {store.get_total('deposited')} (expect 475)")
        rows = [e for e in store.list_events(1000) if e.tx_hash == same_tx]
        print(f"same-tx rows surviving: {len(rows)}; now in block #{rows[0].block_number} "
              f"hash {rows[0].block_hash[:18]}...")
        assert len(rows) == 1
        assert rows[0].block_hash == canon_4["hash"] != uncle_4["hash"]

        # simulated restart: reopen the on-disk database with new objects
        store.close()
        store = Storage(db_path)
        idx = Indexer(w3, store, vault_address=addr, start_block=0, confirmation_depth=0)
        line("RESTART (reopened on-disk database)")
        print(f"tip persisted: #{store.tip().number} {store.tip().hash[:18]}...")
        print(f"user1: {store.get_balance(user1)}  deposited: {store.get_total('deposited')}")
        dep, wit = vault.functions.stats().call()
        ok = store.get_total("deposited") == dep and store.get_total("withdrawn") == wit
        print(f"matches on-chain stats(): {ok}  (deposited={dep}, withdrawn={wit})")
        store.close()

        line("DEMO OK")
        return 0 if ok else 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
