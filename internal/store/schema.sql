-- Task Resource Deadlock Check — PostgreSQL schema
-- All allocation state lives in these tables so a process restart can
-- reconstruct every holder / waiter relationship from durable data.

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tools and workstations are the same kind of lockable object;
-- `kind` distinguishes them. Every resource is an exclusive unit.
CREATE TABLE IF NOT EXISTS resources (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('tool','station')),
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    priority      INTEGER NOT NULL DEFAULT 100,           -- smaller number = higher priority
    state         TEXT NOT NULL CHECK (state IN (
                      'waiting','running','uncertain','revoking',
                      'completed','failed','revoked')),
    deadline_ms   INTEGER NOT NULL DEFAULT 30000,         -- lease duration granted at acquire
    acquired_at   TIMESTAMPTZ,                            -- when the whole group was granted
    lease_expires TIMESTAMPTZ,                            -- heartbeat-maintained running deadline
    enqueued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),     -- start of waiting time (aging)
    revoke_requested_at TIMESTAMPTZ,
    fence_epoch   BIGINT NOT NULL DEFAULT 0,              -- restart epoch; stale tokens are rejected
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);

-- One row per (task, resource) whether the task is still asking for it
-- (wanted) or currently holding it (held). This is the lock table.
CREATE TABLE IF NOT EXISTS task_resources (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL REFERENCES resources(id),
    status      TEXT NOT NULL CHECK (status IN ('wanted','held')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_at   TIMESTAMPTZ,
    PRIMARY KEY (task_id, resource_id)
);
-- At most one holder per resource. Wanted rows deliberately have NULL
-- granted_at so the partial index does not constrain queued waiters.
CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_holder
    ON task_resources(resource_id) WHERE status = 'held';
CREATE INDEX IF NOT EXISTS idx_tr_status ON task_resources(status);

-- Append-only ledger: every grant, wait, release, state change.
-- This is the "resource holding evidence" trail.
CREATE TABLE IF NOT EXISTS hold_ledger (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('granted','released','wanted')),
    fence_epoch BIGINT NOT NULL,
    evidence    TEXT NOT NULL DEFAULT '',               -- signed token for grants
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ledger_task ON hold_ledger(task_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ledger_resource ON hold_ledger(resource_id, occurred_at);

CREATE TABLE IF NOT EXISTS task_events (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, occurred_at);
