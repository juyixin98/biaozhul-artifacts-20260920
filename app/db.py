"""PostgreSQL access: connection pool and idempotent schema creation."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from .config import settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chains (
    id              TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS validators (
    chain_id            TEXT NOT NULL REFERENCES chains(id),
    validator_address   TEXT NOT NULL,
    moniker             TEXT NOT NULL DEFAULT '',
    public_key          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, validator_address)
);

-- A frozen stake base at an epoch boundary. The (chain, validator, epoch) row
-- is immutable: later delegations insert future-epoch rows, never rewrite this.
CREATE TABLE IF NOT EXISTS stake_snapshots (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id            TEXT NOT NULL,
    validator_address   TEXT NOT NULL,
    epoch               BIGINT NOT NULL,
    voting_power        BIGINT NOT NULL CHECK (voting_power >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain_id, validator_address, epoch)
);

-- Every accepted signed vote, with its original wire payload preserved.
CREATE TABLE IF NOT EXISTS votes (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id            TEXT NOT NULL REFERENCES chains(id),
    validator_address   TEXT NOT NULL,
    height              BIGINT NOT NULL,
    round               BIGINT NOT NULL,
    vote_type           TEXT NOT NULL,
    block_hash          TEXT NOT NULL,
    public_key          TEXT NOT NULL,
    signature           TEXT NOT NULL,
    raw_envelope        JSONB NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A given signed message is stored once; late or repeated resubmits
    -- collapse onto the existing row.
    UNIQUE (chain_id, validator_address, height, round, vote_type, block_hash)
);

-- Double-sign evidence, canonicalized and addressed by a content hash.
CREATE TABLE IF NOT EXISTS evidence (
    id                  TEXT PRIMARY KEY,          -- sha256 hex
    chain_id            TEXT NOT NULL,
    validator_address   TEXT NOT NULL,
    height              BIGINT NOT NULL,
    round               BIGINT NOT NULL,
    vote_type           TEXT NOT NULL,
    first_vote_id       BIGINT NOT NULL REFERENCES votes(id),
    second_vote_id      BIGINT NOT NULL REFERENCES votes(id),
    canonical_body      BYTEA NOT NULL,
    raw_evidence        JSONB NOT NULL,            -- original votes preserved
    judgment_version    TEXT NOT NULL,
    epoch               BIGINT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
                            ('DETECTED', 'PENALIZED', 'AWAITING_SNAPSHOT')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Exactly one evidence case per offense group: a third or late conflicting
    -- vote can never open a second punishable case.
    UNIQUE (chain_id, validator_address, height, round, vote_type)
);
CREATE INDEX IF NOT EXISTS evidence_pending_idx
    ON evidence(status, chain_id, validator_address, epoch);

-- One penalty per evidence: the DB constraint is the final guard that late or
-- repeated input can never slash twice.
CREATE TABLE IF NOT EXISTS penalties (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evidence_id         TEXT NOT NULL UNIQUE REFERENCES evidence(id),
    chain_id            TEXT NOT NULL,
    validator_address   TEXT NOT NULL,
    epoch               BIGINT NOT NULL,
    snapshot_id         BIGINT NOT NULL REFERENCES stake_snapshots(id),
    frozen_power        BIGINT NOT NULL,          -- copied, immutable base
    slash_rate_ppm      BIGINT NOT NULL,
    slashed_power       BIGINT NOT NULL,
    judgment_version    TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_pool: ConnectionPool | None = None


def init_pool(database_url: str | None = None, *, open: bool = True) -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = ConnectionPool(
        database_url or settings.database_url,
        min_size=1,
        max_size=10,
        kwargs={"autocommit": False},
        open=open,
    )
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        init_pool()
    assert _pool is not None
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """Short-lived connection inside a committed transaction."""
    pool = get_pool()
    with pool.connection() as conn:
        assert not conn.autocommit
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_SQL)
