"""Shared pytest fixtures: isolated test database and an HTTP TestClient."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Point the app at the test database BEFORE importing app modules.
os.environ.setdefault(
    "DATABASE_URL", "postgresql://epe:epe_dev_pw@localhost:5432/epe_test"
)
os.environ["CRASH_AFTER_EVIDENCE"] = "0"

from app.db import close_pool, get_pool, init_pool, init_schema  # noqa: E402
from app.main import app  # noqa: E402
from app.testsign import make_vote, new_validator  # noqa: E402

TABLES = ["penalties", "evidence", "votes", "stake_snapshots", "validators", "chains"]


@pytest.fixture(scope="session")
def pool():
    p = init_pool()
    with p.connection() as conn:
        init_schema(conn)
        conn.commit()
    yield p
    close_pool()


@pytest.fixture(autouse=True)
def clean_db(pool):
    # Always use the live global pool; the app lifespan rebuilds it after close.
    from app.db import get_pool
    with get_pool().connection() as conn:
        for table in TABLES:
            conn.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
        conn.commit()
    yield


@pytest.fixture
def client():
    # The lifespan already runs against the same shared pool; don't re-init it.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def validator():
    return new_validator()


def register_chain_and_validator(client, chain_id: str, validator: dict, moniker: str = ""):
    r = client.post("/chains", json={"chain_id": chain_id})
    assert r.status_code == 201, r.text
    r = client.post(
        "/validators",
        json={"chain_id": chain_id, "public_key": validator["public_key"], "moniker": moniker},
    )
    assert r.status_code == 201, r.text
    return r.json()


def add_snapshot(client, chain_id: str, validator: dict, epoch: int, power: int):
    r = client.post(
        "/snapshots",
        json={
            "chain_id": chain_id,
            "validator_address": validator["validator_address"],
            "epoch": epoch,
            "voting_power": power,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def signed_vote(validator, *, chain_id, height, round, vote_type, block_hash=None):
    return make_vote(
        validator,
        chain_id=chain_id,
        height=height,
        round=round,
        vote_type=vote_type,
        block_hash=block_hash,
    )
