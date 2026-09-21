#!/usr/bin/env python3
"""End-to-end smoke test against a running stack (default localhost:38080)."""
from __future__ import annotations

import json
import sys

import httpx
from eth_account import Account

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:38080"
PK = "0x079e6e68f056bcd04f10d3ec57ffc8206140e2c43e665aa1464300ad5dc23116"
ADDR = "0x94e1c92dC637df2FA8E49a271DA0446E17ac10e8"
TO = "0x30BB604CCC63a0c8B0E50f7d9117bDA6AEBAA5A8"

c = httpx.Client(base_url=BASE, timeout=10)

r = c.get("/health")
r.raise_for_status()
print("health:", r.json())

r = c.post("/users", json={"name": "e2e-alice"})
r.raise_for_status()
key = r.json()["api_key"]
h = {"Authorization": f"Bearer {key}"}
print("registered; api key never returned again:", "api_key" not in c.get("/wallets", headers=h).text)

r = c.post("/wallets", headers=h, json={"label": "hot", "kind": "custodial", "private_key_hex": PK})
r.raise_for_status()
w = r.json()
assert w["address"] == ADDR and w["has_private_key"] is True
assert PK not in json.dumps(w)
print("custodial wallet:", w["address"])

r = c.post(
    "/drafts", headers=h,
    json={"wallet_id": w["id"], "chain_id": 31337, "to_address": TO,
          "value_wei": 12345678, "gas": 21000, "gas_price_wei": 20 * 10**9,
          "nonce": 5, "data_hex": "0x"},
)
r.raise_for_status()
d = r.json()
print("draft:", d["status"])

r = c.post(f"/sign-requests/{d['id']}/submit", headers=h, json={"idempotency_key": "e2e-order-000001"})
r.raise_for_status()
s1 = r.json()
assert s1["status"] == "success"
raw = s1["raw_transaction_hex"]
print("signed tx_hash:", s1["tx_hash"])

r = c.post(f"/sign-requests/{d['id']}/submit", headers=h, json={"idempotency_key": "e2e-order-000001"})
r.raise_for_status()
s2 = r.json()
assert s2["id"] == s1["id"] and s2["tx_hash"] == s1["tx_hash"] and s2["replayed"] is True
print("idempotent retry returns same result: OK")

recovered = Account.recover_transaction(raw)
assert recovered == ADDR
print("OFFLINE recover_transaction ->", recovered, "matches wallet")

# nonce conflict: different content same nonce
r = c.post("/drafts", headers=h, json={"wallet_id": w["id"], "chain_id": 31337, "to_address": TO,
                                       "value_wei": 999, "gas": 21000, "gas_price_wei": 1, "nonce": 5})
d2 = r.json()
r = c.post(f"/sign-requests/{d2['id']}/submit", headers=h, json={"idempotency_key": "e2e-order-000002"})
assert r.status_code == 409 and r.json()["error"]["code"] == "nonce_conflict", r.text
print("same nonce different content -> 409 nonce_conflict")

# watch-only cannot sign
r = c.post("/wallets", headers=h, json={"label": "obs", "kind": "watch_only",
                                        "address": "0x13510C4584f0a5fc8Fdf26B30a98616746c80e67"})
wo = r.json()
r = c.post("/drafts", headers=h, json={"wallet_id": wo["id"], "chain_id": 1, "to_address": TO,
                                       "value_wei": 1, "gas": 21000, "gas_price_wei": 1, "nonce": 0})
d3 = r.json()
r = c.post(f"/sign-requests/{d3['id']}/submit", headers=h, json={"idempotency_key": "e2e-order-000003"})
assert r.status_code == 403 and r.json()["error"]["code"] == "watch_only_cannot_sign", r.text
print("watch-only sign -> 403")

# private key must not appear in any container-visible API surface checked here
r = c.get("/audit-logs", headers=h)
r.raise_for_status()
assert PK[2:] not in r.text and "raw_transaction" not in r.text
actions = {row["action"] for row in r.json()}
assert {"user.register", "wallet.create", "draft.create", "sign.submit"} <= actions
print("audit trail actions:", sorted(actions))

# cross-user isolation
r = c.post("/users", json={"name": "mallory"})
mk = r.json()["api_key"]
mh = {"Authorization": f"Bearer {mk}"}
assert c.get(f"/wallets/{w['id']}", headers=mh).status_code == 404
assert c.get(f"/sign-requests/{s1['id']}", headers=mh).status_code == 404
print("cross-user access -> 404")

print("\nALL E2E CHECKS PASSED")
