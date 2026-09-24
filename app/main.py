"""FastAPI HTTP surface + background polling loop."""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .chain import ChainClient
from .config import Settings
from .indexer import Indexer
from .store import IndexStore

settings = Settings()
_state: dict[str, object] = {}
_sync_lock = threading.RLock()
_last_report: dict | None = None


def build_app(
    *,
    rpc_url: str | None = None,
    contract_address: str | None = None,
    db_path: str | None = None,
    start_block: int | None = None,
    confirmations: int | None = None,
    poll_interval: float | None = None,
    auto_start: bool | None = None,
) -> FastAPI:
    s = Settings()
    rpc_url = rpc_url or s.rpc_url
    contract_address = contract_address or s.ledger_address
    db_path = db_path or s.db_path
    start_block = s.start_block if start_block is None else start_block
    confirmations = s.confirmations if confirmations is None else confirmations
    poll_interval = s.poll_interval if poll_interval is None else poll_interval
    if auto_start is None:
        auto_start = s.auto_start

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = IndexStore(db_path)
        client = ChainClient(rpc_url, contract_address)
        indexer = Indexer(
            client,
            store,
            start_block=start_block,
            confirmations=confirmations,
            log_chunk_size=s.log_chunk_size,
        )
        stop = threading.Event()

        def run_loop() -> None:
            while not stop.is_set():
                try:
                    with _sync_lock:
                        report = indexer.sync_once()
                        _state["last_report"] = report.as_dict()
                        _state["last_error"] = None
                except Exception as exc:  # noqa: BLE001 - surfaced via /healthz
                    _state["last_error"] = f"{type(exc).__name__}: {exc}"
                stop.wait(poll_interval)

        _state.update(
            store=store,
            client=client,
            indexer=indexer,
            stop=stop,
            thread=None,
            contract_address=contract_address,
            poll_interval=poll_interval,
        )
        if auto_start:
            t = threading.Thread(target=run_loop, name="indexer-loop", daemon=True)
            _state["thread"] = t
            t.start()
        try:
            yield
        finally:
            stop.set()
            t = _state.get("thread")
            if t is not None:
                t.join(timeout=3)
            store.close()

    app = FastAPI(
        title="Event Rollback Indexer",
        version="1.0.0",
        description=(
            "Indexes Ledger contract events from a local Anvil chain, survives "
            "reorgs by inverse event application, and restarts by canonical replay."
        ),
        lifespan=lifespan,
    )

    def _store() -> IndexStore:
        store = _state.get("store")
        if store is None:
            raise HTTPException(503, "indexer not initialized")
        return store  # type: ignore[return-value]

    def _indexer() -> Indexer:
        indexer = _state.get("indexer")
        if indexer is None:
            raise HTTPException(503, "indexer not initialized")
        return indexer  # type: ignore[return-value]

    def _confirmed_number() -> int | None:
        return _indexer().confirmed_tip_number()

    # ------------------------------------------------------------ health

    @app.get("/healthz")
    def healthz() -> dict:
        client = _state.get("client")
        connected = False
        head = None
        if client is not None:
            try:
                head = client.head()  # type: ignore[union-attr]
                connected = True
                head = {"number": head.number, "hash": head.hash}
            except Exception as exc:  # noqa: BLE001
                _state["last_error"] = f"{type(exc).__name__}: {exc}"
        tip = _store().tip()
        return {
            "chain_connected": connected,
            "chain_head": head,
            "indexed_tip": _tip_dict(tip),
            "confirmed_tip_number": _confirmed_number(),
            "contract": _state.get("contract_address"),
            "last_error": _state.get("last_error"),
            "polling": _state.get("thread") is not None
            and _state["thread"].is_alive(),  # type: ignore[union-attr]
        }

    # ------------------------------------------------------------- sync

    @app.post("/sync")
    def trigger_sync() -> dict:
        with _sync_lock:
            report = _indexer().sync_once()
        return report.as_dict()

    @app.get("/status")
    def status() -> dict:
        store = _store()
        tip = store.tip()
        confirmed = _confirmed_number()
        return {
            "tip": _tip_dict(tip),
            "confirmed_tip_number": confirmed,
            "counts": store.block_counts(),
            "last_sync": _state.get("last_report"),
            "last_error": _state.get("last_error"),
        }

    # ------------------------------------------------------------ blocks

    @app.get("/blocks")
    def blocks(canonical_only: bool = Query(True)) -> list[dict]:
        out = _store().list_blocks(canonical_only=canonical_only)
        confirmed = _confirmed_number()
        for row in out:
            row["confirmed"] = confirmed is not None and row["number"] <= confirmed
        return out

    @app.get("/blocks/orphans")
    def orphans() -> list[dict]:
        return _store().list_orphan_blocks()

    # ------------------------------------------------------------ events

    @app.get("/events")
    def events(
        canonical_only: bool = Query(True),
        account: str | None = Query(None),
        confirmed_only: bool = Query(False),
    ) -> list[dict]:
        out = _store().list_events(canonical_only=canonical_only)
        confirmed = _confirmed_number()
        for row in out:
            row["confirmed"] = confirmed is not None and row["block_number"] <= confirmed
        if account is not None:
            a = account.lower()
            out = [
                r
                for r in out
                if r["account"].lower() == a
                or (r.get("to_account") and r["to_account"].lower() == a)
            ]
        if confirmed_only:
            out = [r for r in out if r["confirmed"]]
        return out

    # ------------------------------------------------------------- state

    @app.get("/state")
    def state(account: str | None = Query(None)) -> dict:
        """Materialized balances = fold of canonical events."""
        balances = _store().list_balances()
        if account is not None:
            balances = {account: balances.get(account, 0)}
        return {
            "tip": _tip_dict(_store().tip()),
            "confirmed_tip_number": _confirmed_number(),
            "balances": balances,
        }

    return app


def _tip_dict(tip) -> dict | None:
    if tip is None:
        return None
    return {"number": tip[0], "hash": tip[1]}


app = build_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(__import__("os").environ.get("PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
