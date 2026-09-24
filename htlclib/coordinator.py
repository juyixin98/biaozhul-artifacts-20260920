"""Read-only HTLC coordinator over two local Anvil chains.

This service holds NO private keys and sends NO transactions. It only calls
view functions on both chains and presents a combined picture. All mutation
(lock/claim/refund) goes through the signing client in htlclib/contract.py,
used by the scripts and tests.

Run:
    uvicorn htlclib.coordinator:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from web3 import Web3

from .chains import (
    CHAIN_A_ID,
    CHAIN_A_RPC,
    CHAIN_B_ID,
    CHAIN_B_RPC,
    DELTA_SECONDS,
    TTL_A_SECONDS,
)
from .contract import HtlcClient, STATE_NAMES, derive_swap_id

DEPLOYMENT_FILE = Path(os.environ.get(
    "HTLC_DEPLOYMENT_FILE",
    Path(__file__).resolve().parent.parent / "deployment.json",
))

app = FastAPI(
    title="Dual-chain HTLC read-only coordinator",
    version="1.0.0",
    description=(
        "Read-only view of two independent HashedTimelock contracts on local "
        "Anvil chains A and B. No keys, no writes, no cross-chain messaging — "
        "it does NOT provide arbitrary cross-chain atomicity."
    ),
)


def _load_deployment() -> dict[str, Any]:
    if not DEPLOYMENT_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail=f"deployment file {DEPLOYMENT_FILE} missing; run scripts/deploy.py first",
        )
    return json.loads(DEPLOYMENT_FILE.read_text())


def _chain_specs() -> dict[str, dict[str, Any]]:
    dep = _load_deployment()
    return {
        "A": {"rpc": dep["chain_a"]["rpc"], "chain_id": dep["chain_a"]["chain_id"],
              "address": dep["chain_a"]["address"]},
        "B": {"rpc": dep["chain_b"]["rpc"], "chain_id": dep["chain_b"]["chain_id"],
              "address": dep["chain_b"]["address"]},
    }


def _health(spec: dict[str, Any]) -> dict[str, Any]:
    out = {"rpc": spec["rpc"], "expected_chain_id": spec["chain_id"],
           "contract": spec["address"], "up": False,
           "chain_id": None, "block_number": None, "error": None}
    try:
        w3 = Web3(Web3.HTTPProvider(spec["rpc"], request_kwargs={"timeout": 2}))
        if w3.is_connected():
            out["up"] = True
            out["chain_id"] = w3.eth.chain_id
            out["block_number"] = w3.eth.block_number
            out["chain_id_matches"] = w3.eth.chain_id == spec["chain_id"]
    except Exception as exc:  # node down / refused / timeout
        out["error"] = type(exc).__name__
    return out


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "htlc-coordinator", "docs": "/docs", "health": "/health"}


@app.get("/convention")
def convention() -> dict[str, Any]:
    """The explicit time convention shared by both legs."""
    return {
        "hash": "keccak256(preimage), preimage length 32 bytes",
        "timelock_unit": "block.timestamp, unix seconds",
        "ttl_a_seconds": TTL_A_SECONDS,
        "delta_seconds": DELTA_SECONDS,
        "rule": "T_A = now + TTL_A ; T_B = T_A - DELTA (chain B leg expires first)",
        "state_machine": {
            "LOCKED": ["CLAIMED", "REFUNDED"],
            "CLAIMED": [],
            "REFUNDED": [],
        },
        "claim_rule": "valid preimage while LOCKED always wins, even at/after T",
        "refund_rule": "only locker, only at/after T, only while LOCKED",
        "safety_requirement": (
            "DELTA must exceed the worst-case block production/halt delay on "
            "either chain. A halt longer than DELTA can leave one side CLAIMED "
            "and the other REFUNDED (loss assigned to one party)."
        ),
        "scope": "no cross-chain messaging; arbitrary cross-chain atomicity is NOT solved",
    }


@app.get("/health")
def health() -> JSONResponse:
    specs = _chain_specs()
    chains = {name: _health(spec) for name, spec in specs.items()}
    http_code = 200 if all(c["up"] for c in chains.values()) else 503
    return JSONResponse(status_code=http_code, content={"chains": chains})


@app.get("/chains")
def chains() -> dict[str, Any]:
    return _chain_specs()


def _read_swap(chain: str, swap_id_hex: str) -> dict[str, Any]:
    if chain not in ("A", "B"):
        raise HTTPException(status_code=404, detail="chain must be 'A' or 'B'")
    spec = _chain_specs()[chain]
    try:
        w3 = Web3(Web3.HTTPProvider(spec["rpc"], request_kwargs={"timeout": 2}))
        if not w3.is_connected():
            raise ConnectionError(f"chain {chain} unreachable at {spec['rpc']}")
        client = HtlcClient(w3, spec["address"])
        swap_id = bytes.fromhex(swap_id_hex.removeprefix("0x"))
        if len(swap_id) != 32:
            raise ValueError("swap_id must be 32 bytes")
        view = client.view_swap(swap_id).to_dict()
        view["chain"] = chain
        return view
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        # Node stopped / paused: surface as 502 with the reason instead of a crash
        raise HTTPException(status_code=502,
                            detail=f"chain {chain} unavailable: {type(exc).__name__}: {exc}")


@app.get("/swaps/{chain}/{swap_id}")
def swap(chain: str, swap_id: str) -> dict[str, Any]:
    return _read_swap(chain, swap_id)


def _classify_pair(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    """Pure-read risk classification from the two local states."""
    notes: list[str] = []
    level = "ok"
    if a is None or b is None:
        level = "unreachable"
        notes.append("one chain unreachable: coordinator cannot determine joint state")
        return {"risk_level": level, "notes": notes}

    sa, sb = a["state"], b["state"]
    pair = {sa, sb}

    if sa == "LOCKED" and sb == "LOCKED":
        level = "in_progress"
        notes.append("both legs locked; safe while no preimage is revealed")
    elif sa == "CLAIMED" and sb == "CLAIMED":
        level = "settled"
        notes.append("both legs claimed with the same preimage: swap completed")
    elif sa == "REFUNDED" and sb == "REFUNDED":
        level = "aborted"
        notes.append("both legs refunded: swap aborted, funds back to lockers")
    elif pair <= {"LOCKED", "REFUNDED"} and "REFUNDED" in pair:
        level = "aborting"
        notes.append("one leg refunded after timeout; the other must NOT be claimed "
                     "(reveal now = certain loss). Locker of the LOCKED leg should "
                     "wait for its own refund.")
    elif "CLAIMED" in pair and ("LOCKED" in pair or "REFUNDED" in pair):
        level = "race"
        notes.append("preimage revealed on one side only. Counterparty can still claim "
                     "the other leg while LOCKED. If that chain is halted past T_A, "
                     "the leg may refund first -> one party loses. This is the HTLC "
                     "timeout-gap boundary, NOT solved by this system.")
    elif sa == "NONEXISTENT" or sb == "NONEXISTENT":
        level = "partial"
        notes.append("leg missing on at least one chain")
    return {"risk_level": level, "notes": notes}


@app.get("/pair")
def pair(
    swap_id_a: str | None = None,
    swap_id_b: str | None = None,
    hash_lock: str | None = None,
    sender_a: str | None = None,
    receiver_a: str | None = None,
    sender_b: str | None = None,
    receiver_b: str | None = None,
    amount_wei: int | None = None,
    timelock_a: int | None = None,
) -> dict[str, Any]:
    """Combined read of both legs.

    Give raw ids (swap_id_a/swap_id_b), or give the lock parameters so the
    coordinator derives each id exactly as the contract does.
    """
    try:
        if swap_id_a and swap_id_b:
            id_a = bytes.fromhex(swap_id_a.removeprefix("0x"))
            id_b = bytes.fromhex(swap_id_b.removeprefix("0x"))
        elif all(v is not None for v in (hash_lock, sender_a, receiver_a,
                                         sender_b, receiver_b, amount_wei, timelock_a)):
            hl = bytes.fromhex(hash_lock.removeprefix("0x"))
            id_a = derive_swap_id(hl, sender_a, receiver_a, amount_wei, timelock_a)
            id_b = derive_swap_id(hl, sender_b, receiver_b, amount_wei,
                                  int(timelock_a) - DELTA_SECONDS)
        else:
            raise HTTPException(
                status_code=400,
                detail="provide swap_id_a+swap_id_b, or all of: hash_lock, "
                       "sender_a, receiver_a, sender_b, receiver_b, amount_wei, timelock_a")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad parameters: {exc}")

    result: dict[str, Any] = {"chain_a": None, "chain_b": None}
    for key, sid in (("chain_a", id_a), ("chain_b", id_b)):
        chain = key[-1].upper()
        try:
            result[key] = _read_swap(chain, sid.hex())
        except HTTPException as exc:
            result[key] = {"chain": chain, "error": exc.detail,
                           "risk_level": "unreachable"}

    a = result["chain_a"] if "state" in (result["chain_a"] or {}) else None
    b = result["chain_b"] if "state" in (result["chain_b"] or {}) else None
    result["assessment"] = _classify_pair(a, b)
    result["delta_seconds"] = DELTA_SECONDS
    return result
