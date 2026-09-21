"""Cross-user isolation, fully independent offline signature verification,
and the audit trail."""
from __future__ import annotations

import json

import rlp
from eth_account._utils.legacy_transactions import Transaction
from eth_keys import keys as eth_keys
from eth_keys.backends.native.ecdsa import N as _SECPK1_N
from eth_utils import keccak, to_canonical_address

from tests.conftest import (
    TEST_ADDR,
    auth,
    make_custodial,
    make_draft,
    register,
    submit,
)


# ---------------------------------------------------------------------------
# Independent offline verification (does NOT use eth_account.recover_transaction)
# ---------------------------------------------------------------------------


def _legacy_signing_hash(tx_fields: dict, chain_id: int) -> bytes:
    """RLP([nonce, gasPrice, gasLimit, to, value, data, chainId, 0, 0])"""
    fields = [
        tx_fields["nonce"],
        tx_fields["gas_price_wei"],
        tx_fields["gas"],
        to_canonical_address(tx_fields["to_address"]) if tx_fields["to_address"] else b"",
        tx_fields["value_wei"],
        bytes.fromhex(tx_fields["data_hex"][2:]) if tx_fields["data_hex"] != "0x" else b"",
        chain_id,
        0,
        0,
    ]
    return keccak(rlp.encode(fields))


def _recover_address_independently(raw_hex: str) -> str:
    """Recover the signer address using raw ECDSA public-key recovery from the
    eth_keys primitive, implementing the EIP-155 y-parity rule ourselves."""
    decoded = rlp.decode(bytes.fromhex(raw_hex[2:]), Transaction)
    fields = {
        "nonce": int(decoded.nonce),
        "gas_price_wei": int(decoded.gasPrice),
        "gas": int(decoded.gas),
        "to_address": "0x" + bytes(decoded.to).hex() if decoded.to else "",
        "value_wei": int(decoded.value),
        "data_hex": "0x" + bytes(decoded.data).hex() if decoded.data else "0x",
        "v": int(decoded.v),
        "r": int(decoded.r),
        "s": int(decoded.s),
    }
    v = fields["v"]
    # EIP-155: recovery_id = v - 35 - 2*chain_id  (0 or 1)
    # chain_id is encoded, not derivable from a single legacy tx alone without
    # trying candidates - but v = chain_id*2 + 35/36.
    if v >= 35:
        chain_id = (v - 35) // 2
        recovery_id = v - 35 - 2 * chain_id
    else:
        chain_id = None
        recovery_id = v - 27
    assert recovery_id in (0, 1), fields
    sighash = _legacy_signing_hash(fields, chain_id)
    signature = eth_keys.Signature(
        fields["r"].to_bytes(32, "big")
        + fields["s"].to_bytes(32, "big")
        + bytes([recovery_id])
    )
    public_key = signature.recover_public_key_from_msg_hash(sighash)
    return public_key.to_checksum_address()


def test_offline_signature_verifies_independently(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(
        client,
        key,
        w["id"],
        nonce=5,
        value=12_345_678,
        chain_id=31337,
        gas=21_000,
        gas_price=20 * 10**9,
        data_hex="0x",
    )
    body = submit(client, key, d["id"], "idem-verify-00000001").json()

    # 1. Independent signer recovery must equal the custodial wallet address.
    signer = _recover_address_independently(body["raw_transaction_hex"])
    assert signer == TEST_ADDR

    # 2. tx_hash must be keccak256 of the raw signed transaction.
    raw = bytes.fromhex(body["raw_transaction_hex"][2:])
    assert "0x" + keccak(raw).hex() == body["tx_hash"]

    # 3. Decoded fields must match the frozen draft exactly.
    decoded = rlp.decode(raw, Transaction)
    assert int(decoded.nonce) == 5
    assert int(decoded.value) == 12_345_678
    assert int(decoded.gas) == 21_000
    assert int(decoded.gasPrice) == 20 * 10**9
    assert "0x" + bytes(decoded.to).hex().lower() == d["to_address"].lower()
    # EIP-155: v = 2*chain_id + 35 + y_parity, y_parity in {0,1}.
    parity = int(decoded.v) - (2 * 31337 + 35)
    assert parity in (0, 1)
    # Low-s normalization (EIP-2) is applied by eth-account.
    assert int(decoded.s) <= _SECPK1_N // 2


def test_signature_is_deterministic_and_chain_bound(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d1 = make_draft(client, key, w["id"], nonce=1, chain_id=1)
    d2 = make_draft(client, key, w["id"], nonce=1, chain_id=5)
    h1 = submit(client, key, d1["id"], "idem-chainbind-1-01").json()["tx_hash"]
    h2 = submit(client, key, d2["id"], "idem-chainbind-2-02").json()["tx_hash"]
    assert h1 != h2  # chain_id is part of the signed message

    # Same key + same content -> identical signature bytes.
    d1b = make_draft(client, key, w["id"], nonce=1, chain_id=1)
    h1b = submit(client, key, d1b["id"], "idem-chainbind-3-03").json()
    assert h1b["replayed"] is True
    assert h1b["tx_hash"] == h1


# ------------------------------------------------------------- isolation ----

def test_cannot_read_other_users_draft_or_request(client, no_cooldown):
    _, k1 = register(client, "alice")
    _, k2 = register(client, "bob")
    w1 = make_custodial(client, k1)
    d1 = make_draft(client, k1, w1["id"], nonce=9)
    req = submit(client, k1, d1["id"], "idem-xuser-0000001").json()

    for path in (
        f"/drafts/{d1['id']}",
        f"/sign-requests/{req['id']}",
        f"/wallets/{w1['id']}",
    ):
        r = client.get(path, headers=auth(k2))
        assert r.status_code == 404, path

    # Bob cannot submit/resume/release Alice's resources.
    r = client.post(
        f"/sign-requests/{d1['id']}/submit",
        headers=auth(k2),
        json={"idempotency_key": "bob-try-0000000001"},
    )
    assert r.status_code == 404
    r = client.post(f"/sign-requests/{req['id']}/resume", headers=auth(k2))
    assert r.status_code == 404
    r = client.post(f"/sign-requests/{req['id']}/release", headers=auth(k2))
    assert r.status_code == 404


def test_cannot_list_other_users_requests(client, no_cooldown):
    _, k1 = register(client, "alice")
    _, k2 = register(client, "bob")
    w1 = make_custodial(client, k1)
    d1 = make_draft(client, k1, w1["id"], nonce=9)
    submit(client, k1, d1["id"], "idem-listing-0000001")
    assert client.get("/sign-requests", headers=auth(k2)).json() == []
    # Bob has no signing activity; his only audit entry is his own signup.
    bob_logs = client.get("/audit-logs", headers=auth(k2)).json()
    assert {l["action"] for l in bob_logs} == {"user.register"}
    assert len(client.get("/sign-requests", headers=auth(k1)).json()) == 1


# ----------------------------------------------------------------- audit ----

def test_audit_log_records_summary_without_secrets(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=12, value=42)
    submit(client, key, d["id"], "idem-audit-000000001")
    logs = client.get("/audit-logs", headers=auth(key)).json()
    actions = {l["action"] for l in logs}
    assert "user.register" in actions
    assert "wallet.create" in actions
    assert "draft.create" in actions
    assert "sign.submit" in actions
    sign_log = next(l for l in logs if l["action"] == "sign.submit")
    assert sign_log["result"] == "success"
    summary = json.loads(sign_log["summary"])
    assert summary["nonce"] == 12
    assert summary["value_wei"] == "42"
    assert summary["tx_hash"].startswith("0x")
    # Nothing secret is stored.
    full = json.dumps(logs)
    assert "079e6e68" not in full          # private key
    assert "raw_transaction" not in full   # raw signed tx not needed in audit
    assert "private" not in full.lower()


def test_audit_logs_denials(client):
    from app.config import settings

    settings.daily_quota_wei = 100
    try:
        _, key = register(client)
        w = make_custodial(client, key)
        d = make_draft(client, key, w["id"], nonce=1, value=500)
        r = submit(client, key, d["id"], "idem-auditden-000001")
        assert r.status_code == 429
        logs = client.get("/audit-logs", headers=auth(key)).json()
        denied = [l for l in logs if l["result"] == "denied"]
        assert len(denied) == 1
        assert json.loads(denied[0]["summary"])["requested_wei"] == "500"
    finally:
        settings.daily_quota_wei = 10**18
