import base64
import os
import tempfile

# Environment must be set before any app module is imported.
_tmpdir = tempfile.mkdtemp(prefix="vaultcommand-test-")
os.environ["VAULT_DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["VAULT_MASTER_KEY"] = base64.b64encode(b"k" * 32).decode()
os.environ["VAULT_DAILY_LIMIT_WEI"] = str(10**18)
os.environ["VAULT_COOLDOWN_SECONDS"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402,F401

Base.metadata.create_all(engine)

# Well-known Hardhat dev key #0 (test only).
TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


@pytest.fixture(autouse=True)
def clean_db():
    yield
    db = SessionLocal()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def client():
    return TestClient(app)


def make_user(client) -> str:
    r = client.post("/users")
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def auth(key: str) -> dict:
    return {"X-API-Key": key}


def make_custodial_wallet(client, key, private_key=TEST_PRIVATE_KEY) -> dict:
    body = {"kind": "custodial"}
    if private_key is not None:
        body["private_key"] = private_key
    r = client.post("/wallets", json=body, headers=auth(key))
    assert r.status_code == 201, r.text
    return r.json()


def make_watch_wallet(client, key, address=TEST_ADDRESS) -> dict:
    r = client.post("/wallets", json={"kind": "watch_only", "address": address}, headers=auth(key))
    assert r.status_code == 201, r.text
    return r.json()


def make_draft(client, key, wallet_id, *, nonce=0, chain_id=1, to=TEST_ADDRESS,
               value_wei="100000000000000", gas_limit=21000, gas_price_wei="1000000000") -> dict:
    r = client.post(
        "/drafts",
        json={
            "wallet_id": wallet_id,
            "chain_id": chain_id,
            "to_address": to,
            "value_wei": value_wei,
            "gas_limit": gas_limit,
            "gas_price_wei": gas_price_wei,
            "nonce": nonce,
        },
        headers=auth(key),
    )
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, key, draft_id) -> dict:
    r = client.post(f"/drafts/{draft_id}/submit", headers=auth(key))
    assert r.status_code == 200, r.text
    return r.json()


def sign(client, key, draft_id, idem_key):
    return client.post(
        "/sign",
        json={"draft_id": draft_id},
        headers={**auth(key), "Idempotency-Key": idem_key},
    )
