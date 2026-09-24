-- Behavior tree resumable execution — schema (migration 0001)

CREATE TABLE IF NOT EXISTS schema_migrations (
    version  INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Published tree definitions. Immutable once published; content-addressed
-- by the SHA-256 of the canonical JSON encoding (definition_hash). A tree id
-- identifies a name lineage: the same logical tree republished after a
-- change reuses its id and gets the next name-scoped version, so the primary
-- key is the composite (id, version).
CREATE TABLE IF NOT EXISTS trees (
    id              TEXT NOT NULL,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    definition_hash TEXT NOT NULL,
    definition      JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, version),
    UNIQUE (name, version),
    UNIQUE (definition_hash)
);

-- One execution binds one immutable published tree version (composite FK).
CREATE TABLE IF NOT EXISTS executions (
    id           TEXT PRIMARY KEY,
    tree_id      TEXT NOT NULL,
    tree_version INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('running', 'success', 'failure', 'aborted')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tree_id, tree_version) REFERENCES trees(id, version)
);
CREATE INDEX IF NOT EXISTS idx_executions_status ON executions(status);

-- Durable per-execution monotonically increasing tick sequence.
-- Every tick (including interrupted ones) consumes exactly one sequence value.
CREATE TABLE IF NOT EXISTS ticks (
    id           TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    seq          BIGINT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('completed', 'interrupted')),
    tree_status  TEXT CHECK (tree_status IN ('running', 'success', 'failure', 'aborted')),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at     TIMESTAMPTZ,
    UNIQUE (execution_id, seq)
);

-- Latched node state keyed by stable node id. Node ids are unique within a
-- tree definition and never move between versions.
CREATE TABLE IF NOT EXISTS node_states (
    execution_id TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    node_id      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('running', 'success', 'failure', 'canceled')),
    updated_seq  BIGINT NOT NULL,
    deadline_at  TIMESTAMPTZ, -- timeout node: wall-clock deadline, persisted so restarts keep it
    PRIMARY KEY (execution_id, node_id)
);

-- One row per (execution, action node): the dedup latch for physical stub
-- execution. dedup_key = execution_id || ':' || node_id is the stable
-- identity; attempt fences late results from a previous physical attempt.
CREATE TABLE IF NOT EXISTS action_calls (
    execution_id   TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    node_id        TEXT NOT NULL,
    id             TEXT NOT NULL,
    stub           TEXT NOT NULL,
    non_idempotent BOOLEAN NOT NULL DEFAULT FALSE,
    attempt        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL CHECK (status IN ('running', 'success', 'failure', 'canceled', 'interrupted')),
    result         JSONB,
    error          TEXT NOT NULL DEFAULT '',
    launched_seq   BIGINT NOT NULL,
    updated_seq    BIGINT NOT NULL,
    PRIMARY KEY (execution_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_action_calls_status ON action_calls(status);

-- Audit row per physical stub execution attempt. Successful non-idempotent
-- stubs are never physically invoked twice, so this table is the proof.
CREATE TABLE IF NOT EXISTS stub_invocations (
    id            BIGSERIAL PRIMARY KEY,
    execution_id  TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    call_id       TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    stub          TEXT NOT NULL,
    mode          TEXT NOT NULL CHECK (mode IN ('sync', 'async')),
    outcome       TEXT CHECK (outcome IN ('success', 'failure')),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_stub_invocations_dedup
    ON stub_invocations(execution_id, node_id, attempt);
