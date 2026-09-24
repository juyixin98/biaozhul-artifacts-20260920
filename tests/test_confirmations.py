"""Confirmation-depth flag semantics (single node, no forks)."""

from __future__ import annotations

import pytest

from app.abi import LEDGER_ABI
from app.chain import ChainClient
from app.config import DEFAULT_TEST_KEY
from app.indexer import Indexer
from app.store import IndexStore

from .conftest import build_tx, send_mined
from .test_reorg import deploy_on


@pytest.mark.anvil
def test_confirmation_flags(node_a, tmp_path):
    w3 = node_a.w3
    address, ledger, acct = deploy_on(node_a)

    store = IndexStore(tmp_path / "idx.db")
    indexer = Indexer(
        ChainClient(node_a.rpc, address), store, start_block=0, confirmations=3
    )
    indexer.sync_once()  # head 1
    assert indexer.confirmed_tip_number() is None  # 1 - 2 < 0

    send_mined(w3, acct, build_tx(w3, acct, ledger, "deposit", [100, 1]), node_a)  # 2
    send_mined(w3, acct, build_tx(w3, acct, ledger, "deposit", [100, 2]), node_a)  # 3
    indexer.sync_once()  # head 3, confirmed up to block 1
    assert indexer.confirmed_tip_number() == 1

    send_mined(w3, acct, build_tx(w3, acct, ledger, "deposit", [100, 3]), node_a)  # 4
    indexer.sync_once()  # head 4, confirmed up to block 2
    assert indexer.confirmed_tip_number() == 2

    events = store.list_events()
    by_tag = {int(e["tag"]): e for e in events}
    confirmed_n = indexer.confirmed_tip_number()
    assert by_tag[1]["block_number"] == 2
    assert by_tag[1]["block_number"] <= confirmed_n
    assert by_tag[2]["block_number"] > confirmed_n
    assert by_tag[3]["block_number"] > confirmed_n
