"""FastAPI service: HTTP query API + background indexer poller.

Configuration (environment variables, with defaults for local Anvil):

    RPC_URL             http://127.0.0.1:8545
    DATABASE_PATH       data/indexer.db   (use :memory: only for tests)
    VAULT_ADDRESS       required for polling (auto-deploy handled by tests/demo)
    START_BLOCK         0
    CONFIRMATION_DEPTH  1
    POLL_INTERVAL       2.0  seconds

Run:

    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import onchain
from .indexer import Indexer
from .storage import Storage

RPC_URL = os.environ.get("RPC_URL", "http://127.0.0.1:8545")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/indexer.db")
VAULT_ADDRESS = os.environ.get("VAULT_ADDRESS", "").strip()
START_BLOCK = int(os.environ.get("START_BLOCK", "0"))
CONFIRMATION_DEPTH = int(os.environ.get("CONFIRMATION_DEPTH", "1"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2.0"))


class _Poller:
    def __init__(self, indexer: Indexer, interval: float):
        self.indexer = indexer
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_report: dict | None = None
        self.last_error: str | None = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                report = self.indexer.poll_once()
                if report is not None:
                    self.last_report = report.__dict__
                    self.last_error = None
            except Exception as exc:  # never let the poller die silently
                self.last_error = repr(exc)

    def trigger(self) -> dict:
        report = self.indexer.poll_once()
        return report.__dict__ if report is not None else {"noop": True}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="indexer-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def build_indexer() -> tuple[Storage, Indexer]:
    if DATABASE_PATH != ":memory:":
        os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    store = Storage(DATABASE_PATH)
    w3 = onchain.make_w3(RPC_URL)
    indexer = Indexer(
        w3,
        store,
        vault_address=VAULT_ADDRESS or "0x" + "00" * 20,
        start_block=START_BLOCK,
        confirmation_depth=CONFIRMATION_DEPTH,
    )
    return store, indexer


@asynccontextmanager
async def lifespan(app: FastAPI):
    store, indexer = build_indexer()
    poller = _Poller(indexer, POLL_INTERVAL)
    app.state.store = store
    app.state.indexer = indexer
    app.state.poller = poller
    app.state.config = {
        "rpc_url": RPC_URL,
        "database_path": DATABASE_PATH,
        "vault_address": VAULT_ADDRESS or None,
        "start_block": START_BLOCK,
        "confirmation_depth": CONFIRMATION_DEPTH,
        "poll_interval": POLL_INTERVAL,
    }
    if VAULT_ADDRESS:
        poller.start()
        # one immediate sync so GETs return data without waiting a cycle
        threading.Thread(target=poller.trigger, daemon=True).start()
    yield
    poller.stop()
    store.close()


app = FastAPI(title="Event-Rollback Indexer", version="1.0.0", lifespan=lifespan)


class BalanceOut(BaseModel):
    who: str
    balance: str


class TotalsOut(BaseModel):
    deposited: str
    withdrawn: str
    net_locked: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def config():
    return app.state.config


@app.get("/status")
def status():
    idx: Indexer = app.state.indexer
    tip = idx.store.tip()
    try:
        chain_head = idx.w3.eth.block_number
        connected = True
    except Exception:
        chain_head = None
        connected = False
    return {
        "chain_connected": connected,
        "chain_head": chain_head,
        "target_head": idx.target_head() if connected else None,
        "indexed_tip_number": tip.number if tip else None,
        "indexed_tip_hash": tip.hash if tip else None,
        "confirmation_depth": idx.confirmation_depth,
        "blocks_indexed": idx.store.block_count(),
        "events_indexed": idx.store.event_count(),
        "last_sync": app.state.poller.last_report,
        "last_error": app.state.poller.last_error,
    }


@app.post("/sync")
def sync_now():
    """Trigger an immediate poll (rollback + catch-up) and return the report."""
    try:
        return app.state.poller.trigger()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=repr(exc)) from exc


@app.get("/balance/{address}")
def get_balance(address: str):
    try:
        who = __import__("web3").Web3.to_checksum_address(address)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad address: {exc}") from exc
    return BalanceOut(who=who, balance=str(app.state.store.get_balance(who)))


@app.get("/totals", response_model=TotalsOut)
def totals():
    store = app.state.store
    dep = store.get_total("deposited")
    wit = store.get_total("withdrawn")
    return TotalsOut(deposited=str(dep), withdrawn=str(wit), net_locked=str(dep - wit))


@app.get("/events")
def events(who: str | None = None, limit: int = 100):
    store = app.state.store
    limit = max(1, min(limit, 1000))
    if who:
        try:
            who = __import__("web3").Web3.to_checksum_address(who)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"bad address: {exc}") from exc
        rows = store.events_for(who)[:limit]
    else:
        rows = store.list_events(limit)
    return [
        {
            "block_number": e.block_number,
            "block_hash": e.block_hash,
            "tx_hash": e.tx_hash,
            "tx_index": e.tx_index,
            "log_index": e.log_index,
            "event": e.event_name,
            "who": e.who,
            "amount": str(e.amount),
        }
        for e in rows
    ]


@app.get("/debug")
def debug():
    """Full indexed chain + state. Used by the acceptance tests and demos."""
    return app.state.store.debug_rows()


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
