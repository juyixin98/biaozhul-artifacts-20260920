-- Task resource deadlock checker — schema (version 1)
-- All resource allocation decisions are serialized with a single
-- pg_advisory_xact_lock taken inside every allocator transaction,
-- so the data below is always a consistent global snapshot.

CREATE TABLE IF NOT EXISTS resources (
    kind        TEXT NOT NULL CHECK (kind IN ('tool', 'station')),
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, name)
);

CREATE TABLE IF NOT EXISTS tasks (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label           TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 0),
    -- resource age while waiting, i.e. how much priority one second of
    -- waiting is worth. 0 disables aging for this task.
    aging_per_sec   DOUBLE PRECISION NOT NULL DEFAULT 1.0
        CHECK (aging_per_sec >= 0),
    timeout_ms      BIGINT NOT NULL DEFAULT 30000 CHECK (timeout_ms > 0),
    state           TEXT NOT NULL DEFAULT 'waiting'
        CHECK (state IN ('waiting', 'running', 'uncertain', 'complete', 'failed')),
    -- request currently being served: NULL means the task's initial request
    -- was granted and it is running without a pending dynamic request.
    -- The FK to requests is added below (the two tables reference each
    -- other, so the constraint is created after both exist).
    pending_request_id BIGINT,
    deadline        TIMESTAMPTZ,           -- set only while running
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS requests (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id     BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('initial', 'extra')),
    state       TEXT NOT NULL CHECK (state IN ('waiting', 'granted', 'rejected')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_at  TIMESTAMPTZ
);

-- Deferred circular FK: tasks.pending_request_id -> requests.id.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'tasks_pending_request_fk'
    ) THEN
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_pending_request_fk
            FOREIGN KEY (pending_request_id)
            REFERENCES requests(id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

-- One row per (request, resource): the declared claim.
CREATE TABLE IF NOT EXISTS request_items (
    request_id  BIGINT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (request_id, kind, name),
    FOREIGN KEY (kind, name) REFERENCES resources(kind, name)
);

-- A held resource. Inserting/removing a hold is the actual allocation.
CREATE TABLE IF NOT EXISTS holds (
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    task_id      BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    request_id   BIGINT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, name)
);

CREATE INDEX IF NOT EXISTS holds_task_idx ON holds (task_id);

-- Wait-for edges: waiter task -> holder task, one row per contended resource
-- class (duplicate holders of the same resource cannot exist due to the
-- holds primary key). Rebuilt from scratch on every allocator pass, so the
-- graph is derivable from holds + pending requests but persisted for
-- restart recovery and direct inspection.
CREATE TABLE IF NOT EXISTS wait_edges (
    waiter_task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    holder_task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    resource_kind  TEXT NOT NULL,
    resource_name  TEXT NOT NULL,
    PRIMARY KEY (waiter_task_id, holder_task_id, resource_kind, resource_name)
);

CREATE INDEX IF NOT EXISTS wait_edges_waiter_idx ON wait_edges (waiter_task_id);

-- Lifecycle/allocator audit trail.
CREATE TABLE IF NOT EXISTS task_events (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id    BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    event      TEXT NOT NULL,
    detail     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS task_events_task_idx ON task_events (task_id, id);

-- Tamper-evident ledger of every allocation decision. The canonical payload
-- is signed with HMAC-SHA256 (see internal/evidence); signature verification
-- lets an auditor prove a holder/resource/request record was not altered.
CREATE TABLE IF NOT EXISTS audit_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    event           TEXT NOT NULL,
    task_id         BIGINT,
    request_id      BIGINT,
    resources       JSONB NOT NULL DEFAULT '[]'::jsonb,
    canonical       TEXT NOT NULL,
    sig             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_events_task_idx ON audit_events (task_id, id);

-- Service-wide key/value store: HMAC signing key and schema version.
CREATE TABLE IF NOT EXISTS service_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

INSERT INTO service_meta (key, value)
VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;
