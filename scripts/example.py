"""End-to-end demo against a RUNNING stack (anvil + deploy + API).

Pre-reqs (three terminals, or see README for one-line equivalents):
    anvil --chain-id 31337
    python -m scripts.deploy
    uvicorn app.main:app --port 8000

Then:
    python -m scripts.example

The script talks ONLY to the HTTP API (it does not touch the chain directly),
showing: create+sign an order, two partial fills with rounding, a replay
attempt, and a fresh order that gets cancelled before filling.
"""
from __future__ import annotations

import os
import sys
import time

import httpx

API = os.getenv("API_URL", "http://127.0.0.1:8000")


def main() -> int:
    c = httpx.Client(base_url=API, timeout=30)

    health = c.get("/health").json()
    cfg = c.get("/config").json()
    print(f"== chain {health['chainId']} @ block {health['blockNumber']}")
    print(f"   settlement {cfg['settlement']}")
    print(f"   maker      {cfg['accounts']['maker']}")
    print(f"   taker      {cfg['accounts']['taker']}")

    # 1) create + sign a bounded order: 100 A for 3 B
    nonce = int(time.time())
    created = c.post("/orders", json={
        "makerAmount": 100,
        "takerAmount": 3,
        "nonce": nonce,
        "deadline": int(time.time()) + 3600,
    }).json()
    order = created["order"]
    order["signature"] = created["signature"]
    print(f"\n== order {created['orderHash'][:18]}...  100 A -> 3 B, nonce {nonce}")

    # 2) two partial fills (ceil rounding: 33 A still costs 1 B)
    for spent in (33, 33):
        r = c.post("/orders/fill", json={"order": order, "spent": spent})
        r.raise_for_status()
        d = r.json()
        print(f"   fill {spent:>3} A -> taker paid {d['takerDue']} B, "
              f"spent so far {d['fillStatus']['filledMakerAmount']}")

    # 3) final dust-clearing fill
    r = c.post("/orders/fill", json={"order": order, "spent": 34})
    r.raise_for_status()
    d = r.json()
    print(f"   fill  34 A -> taker paid {d['takerDue']} B (final, clears remainder)")

    # 4) replay attempt must be rejected
    r = c.post("/orders/fill", json={"order": order, "spent": 1})
    print(f"   replay after full fill -> HTTP {r.status_code}: {r.json()['detail'][:60]}")

    # 5) a fresh order that is cancelled before any fill
    nc2 = nonce + 1
    created2 = c.post("/orders", json={
        "makerAmount": 50, "takerAmount": 1, "nonce": nc2,
        "deadline": int(time.time()) + 3600,
    }).json()
    c.post("/orders/cancel", json={"nonce": nc2}).raise_for_status()
    o2 = created2["order"]; o2["signature"] = created2["signature"]
    r = c.post("/orders/fill", json={"order": o2, "spent": 50})
    print(f"   fill after cancel      -> HTTP {r.status_code}: {r.json()['detail'][:60]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
