-- Resumable behavior-tree persistence schema.
-- All behavioral state needed to resume an execution lives here.

CREATE TABLE IF NOT EXISTS trees (
    name        TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Immutable published versions. content_hash is unique: publishing the same
-- definition twice returns the same row, never a new version.
CREATE TABLE IF NOT EXISTS tree_versions (
    name            TEXT NOT NULL REFERENCES trees(name),
    version         BIGINT NOT NULL,
    content_hash    TEXT NOT NULL,
    definition      JSONB NOT NULL,
    published_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version),
    UNIQUE (name, content_hash)
);

-- 'running' | 'success' | 'failure' | 'canceled'
CREATE TABLE IF NOT EXISTS executions (
    id               UUID PRIMARY KEY,
    tree_name        TEXT NOT NULL,
    tree_version     BIGINT NOT NULL,
    content_hash     TEXT NOT NULL,
    status           TEXT NOT NULL,
    next_tick        BIGINT NOT NULL DEFAULT 1,
    last_tick_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at     TIMESTAMPTZ,
    FOREIGN KEY (tree_name, tree_version)
        REFERENCES tree_versions(name, version)
);

CREATE INDEX IF NOT EXISTS idx_executions_status ON executions(status);

-- One row per tick attempt. A tick is inserted as 'running'; it is updated in
-- place on commit. If the tick's context is canceled before commit the whole
-- transaction (including this row) rolls back, and a separate 'interrupted'
-- row is written afterwards, so interrupted attempts remain auditable without
-- consuming the tick sequence. The partial unique index guarantees at most
-- one authoritative row per committed seq.
CREATE TABLE IF NOT EXISTS ticks (
    id             BIGSERIAL,
    execution_id   UUID NOT NULL REFERENCES executions(id),
    seq            BIGINT NOT NULL,
    status         TEXT NOT NULL,                 -- running|success|failure|canceled|interrupted
    note           TEXT NOT NULL DEFAULT '',
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    committed_at   TIMESTAMPTZ,
    interrupted_at TIMESTAMPTZ,
    PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ticks_committed_seq
    ON ticks(execution_id, seq)
    WHERE status <> 'interrupted';

-- Persisted node status, keyed by execution. detail is node-kind specific
-- state such as the timeout deadline or the currently-active child index.
CREATE TABLE IF NOT EXISTS node_states (
    execution_id   UUID NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    node_id        TEXT NOT NULL,
    status         TEXT NOT NULL,                 -- running|success|failure
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_tick   BIGINT NOT NULL,
    PRIMARY KEY (execution_id, node_id)
);

-- 'pending' | 'running' | 'success' | 'failure' | 'canceled'
-- attempt is the epoch: every (re)dispatch bumps it. Late worker reports are
-- accepted only when they carry the current attempt, so stale results from a
-- superseded or canceled dispatch can never resurrect a tree.
CREATE TABLE IF NOT EXISTS invocations (
    execution_id   UUID NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    node_id        TEXT NOT NULL,
    stable_key     TEXT NOT NULL,                 -- deterministic per (execution,node)
    action         TEXT NOT NULL,
    idempotent     BOOLEAN NOT NULL,
    params         JSONB NOT NULL,
    status         TEXT NOT NULL,
    attempt        BIGINT NOT NULL DEFAULT 0,
    result         JSONB,
    error          TEXT NOT NULL DEFAULT '',
    dispatches     BIGINT NOT NULL DEFAULT 0,     -- real dispatch count (dedup proof)
    created_tick   BIGINT NOT NULL,
    updated_tick   BIGINT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (execution_id, stable_key)
);

CREATE INDEX IF NOT EXISTS idx_invocations_status ON invocations(status);
