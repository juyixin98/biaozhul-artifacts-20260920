"""Pytest configuration: isolated test database and API client helpers."""
from __future__ import annotations

import os

# Configure the environment BEFORE importing application modules.
os.environ.setdefault(
    "VAULT_DATABASE_URL",
    "postgresql+psycopg2://vault:vault@localhost:5544/vaulttest",
)
os.environ.setdefault(
    "VAULT_MASTER_KEY_B64",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # base64(32 zero bytes)
)
os.environ.setdefault("VAULT_ENVIRONMENT", "development")
os.environ.setdefault("VAULT_ALLOW_REGISTRATION", "true")
os.environ.setdefault("VAULT_COOLDOWN_SECONDS", "0")
os.environ.setdefault("VAULT_DAILY_QUOTA_WEI", str(10**18))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema():
    """Apply migrations once per test session via Alembic (also tests them)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _restore_settings():
    """Always restore mutable runtime settings, even if a test fails midway."""
    quota = settings.daily_quota_wei
    cooldown = settings.cooldown_seconds
    allow = settings.allow_registration
    yield
    settings.daily_quota_wei = quota
    settings.cooldown_seconds = cooldown
    settings.allow_registration = allow


@pytest.fixture(autouse=True)
def clean_db():
    """Reset every table before each test. users cascades to all children."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_cooldown():
    old = settings.cooldown_seconds
    settings.cooldown_seconds = 0
    yield
    settings.cooldown_seconds = old


# Throwaway deterministic test key (see test-keys.sample.json).
TEST_PK = "0x079e6e68f056bcd04f10d3ec57ffc8206140e2c43e665aa1464300ad5dc23116"
TEST_ADDR = "0x94e1c92dC637df2FA8E49a271DA0446E17ac10e8"
WATCH_ADDR = "0x13510C4584f0a5fc8Fdf26B30a98616746c80e67"
RECIPIENT = "0x30BB604CCC63a0c8B0E50f7d9117bDA6AEBAA5A8"


def register(client: TestClient, name: str = "alice") -> tuple[str, str]:
    r = client.post("/users", json={"name": name})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["id"], body["api_key"]


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def make_custodial(client: TestClient, key: str, label: str = "hot") -> dict:
    r = client.post(
        "/wallets",
        headers=auth(key),
        json={"label": label, "kind": "custodial", "private_key_hex": TEST_PK},
    )
    assert r.status_code == 201, r.text
    return r.json()


def make_watch_only(client: TestClient, key: str, label: str = "observe") -> dict:
    r = client.post(
        "/wallets",
        headers=auth(key),
        json={"label": label, "kind": "watch_only", "address": WATCH_ADDR},
    )
    assert r.status_code == 201, r.text
    return r.json()


def make_draft(
    client: TestClient,
    key: str,
    wallet_id: str,
    *,
    nonce: int = 0,
    value: int = 10**15,
    chain_id: int = 31337,
    gas_price: int = 20 * 10**9,
    gas: int = 21_000,
    to: str = RECIPIENT,
    data_hex: str = "0x",
) -> dict:
    r = client.post(
        "/drafts",
        headers=auth(key),
        json={
            "wallet_id": wallet_id,
            "chain_id": chain_id,
            "to_address": to,
            "value_wei": value,
            "gas": gas,
            "gas_price_wei": gas_price,
            "nonce": nonce,
            "data_hex": data_hex,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def submit(
    client: TestClient, key: str, draft_id: str, idem: str
):
    return client.post(
        f"/sign-requests/{draft_id}/submit",
        headers=auth(key),
        json={"idempotency_key": idem},
    )
