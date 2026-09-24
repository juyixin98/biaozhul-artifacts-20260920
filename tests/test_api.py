"""HTTP API smoke/behavior tests against an in-process FastAPI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.abi import LEDGER_ABI
from app.config import DEFAULT_TEST_KEY
from app.main import build_app

from .conftest import build_tx, send_mined
from .test_reorg import deploy_on

BOB = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


@pytest.mark.anvil
def test_api_endpoints(node_a, tmp_path):
    w3 = node_a.w3
    address, ledger, acct = deploy_on(node_a)
    send_mined(w3, acct, build_tx(w3, acct, ledger, "deposit", [500, 11]), node_a)
    send_mined(w3, acct, build_tx(w3, acct, ledger, "transfer", [BOB, 120, 12]), node_a)

    app = build_app(
        rpc_url=node_a.rpc,
        contract_address=address,
        db_path=str(tmp_path / "api.db"),
        start_block=0,
        confirmations=1,
        auto_start=False,
    )
    with TestClient(app) as client:
        r = client.post("/sync")
        assert r.status_code == 200
        body = r.json()
        assert body["reorg"] is False
        assert body["ingested_events"] == 2  # deposit + transfer

        h = client.get("/healthz").json()
        assert h["chain_connected"] is True
        assert h["indexed_tip"]["number"] == w3.eth.block_number

        state = client.get("/state").json()
        assert int(state["balances"][acct.address]) == 380
        assert int(state["balances"][BOB]) == 120

        events = client.get("/events").json()
        assert [e["name"] for e in events].count("Transferred") == 1
        only_bob = client.get("/events", params={"account": BOB}).json()
        assert all(
            e["account"] == BOB or e.get("to_account") == BOB for e in only_bob
        )

        blocks = client.get("/blocks").json()
        assert all(b["canonical"] == 1 for b in blocks)
        assert len(blocks) == w3.eth.block_number + 1

        status = client.get("/status").json()
        assert status["counts"]["blocks_canonical"] == w3.eth.block_number + 1
        assert status["counts"]["events_rows"] == 2
        assert status["last_error"] is None
