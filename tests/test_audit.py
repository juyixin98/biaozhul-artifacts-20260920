"""Audit trail and migration smoke tests."""

import os
import subprocess
import sys

from conftest import TEST_PRIVATE_KEY, auth, make_custodial_wallet, make_draft, make_user, sign, submit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_audit_trail_records_digests_not_keys(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key, TEST_PRIVATE_KEY)
    draft = make_draft(client, key, wallet["id"])
    submit(client, key, draft["id"])
    r = sign(client, key, draft["id"], "aud-1")
    assert r.status_code == 200
    tx_hash = r.json()["tx_hash"]

    audit = client.get("/audit", headers=auth(key))
    assert audit.status_code == 200
    rows = audit.json()
    actions = [row["action"] for row in rows]
    assert "wallet_created" in actions
    assert "draft_created" in actions
    assert "draft_submitted" in actions
    assert "sign_reserved" in actions
    assert "sign_succeeded" in actions

    succeeded = next(row for row in rows if row["action"] == "sign_succeeded")
    assert succeeded["tx_digest"] == tx_hash
    assert succeeded["result"] == "ok"

    # No key material anywhere in the audit trail.
    assert TEST_PRIVATE_KEY not in audit.text
    assert TEST_PRIVATE_KEY[2:] not in audit.text


def test_audit_isolated_per_user(client):
    key_a = make_user(client)
    key_b = make_user(client)
    wallet = make_custodial_wallet(client, key_a)
    make_draft(client, key_a, wallet["id"])
    assert client.get("/audit", headers=auth(key_b)).json() == []


def test_alembic_upgrade_head(tmp_path):
    db_file = tmp_path / "migrated.db"
    env = dict(
        os.environ,
        VAULT_DATABASE_URL=f"sqlite:///{db_file}",
        VAULT_MASTER_KEY="",
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    import sqlite3

    tables = {
        row[0]
        for row in sqlite3.connect(db_file).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"users", "wallets", "drafts", "sign_requests", "daily_usage", "audit_log"} <= tables
