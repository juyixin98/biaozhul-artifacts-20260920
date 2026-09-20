-- SIRCC initial schema
-- All role/stage/status values are stored as TEXT with CHECK constraints so
-- sqlc generates plain Go strings (no pgtype enums to manage).

CREATE TABLE users (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('analyst','responder','admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE incidents (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    severity          TEXT NOT NULL CHECK (severity IN ('P1','P2','P3','P4')),
    stage             TEXT NOT NULL DEFAULT 'detected'
        CHECK (stage IN ('detected','triaged','contained','eradicated','recovered','postmortem','closed')),
    version           BIGINT NOT NULL DEFAULT 1,
    created_by        TEXT NOT NULL REFERENCES users(id),
    assigned_analyst_id   TEXT REFERENCES users(id),
    assigned_responder_id TEXT REFERENCES users(id),
    -- Postmortem gate fields
    root_cause        TEXT,
    lessons_learned   TEXT,
    -- Timestamps for the stage currently entered. Only VALID (non-skipped)
    -- transitions populate these; they are carried across rows rather than
    -- overwritten (see stage_events).
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    triaged_at     TIMESTAMPTZ,
    contained_at   TIMESTAMPTZ,
    eradicated_at  TIMESTAMPTZ,
    recovered_at   TIMESTAMPTZ,
    postmortem_at  TIMESTAMPTZ,
    closed_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Who may access an incident. Non-admin users must have a row here for every
-- incident-scoped operation (assignment adds the assignee).
CREATE TABLE incident_members (
    incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (incident_id, user_id)
);

-- One row per attempted transition. Stored versions are monotonically
-- increasing; a row missing for an endpoint means the corresponding stage
-- time is invalid for duration calculations.
CREATE TABLE stage_events (
    id              TEXT PRIMARY KEY,
    incident_id     TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    version         BIGINT NOT NULL,
    from_stage      TEXT NOT NULL,
    to_stage        TEXT NOT NULL,
    actor_id        TEXT NOT NULL REFERENCES users(id),
    note            TEXT NOT NULL DEFAULT '',
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id      TEXT NOT NULL,
    UNIQUE (incident_id, version),
    UNIQUE (incident_id, request_id)
);

CREATE INDEX idx_stage_events_incident ON stage_events(incident_id, version);

CREATE TABLE evidence (
    id           TEXT PRIMARY KEY,
    incident_id  TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    content      TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id   TEXT NOT NULL UNIQUE
);

CREATE INDEX idx_evidence_incident ON evidence(incident_id, created_at);

-- Corrections never overwrite evidence; they are appended as linked notes.
CREATE TABLE evidence_notes (
    id           TEXT PRIMARY KEY,
    evidence_id  TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    content      TEXT NOT NULL,
    added_by     TEXT NOT NULL REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id   TEXT NOT NULL UNIQUE
);

CREATE TABLE action_items (
    id            TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    owner_id      TEXT NOT NULL REFERENCES users(id),
    due_at        TIMESTAMPTZ NOT NULL,
    -- Bumped on every reschedule. Reminders key on this version.
    version       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
    created_by    TEXT NOT NULL REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_action_items_open_due ON action_items(status, due_at);

-- Append-only audit of reschedules so the reminder scheduler can tell the
-- current schedule from stale ones.
CREATE TABLE action_item_reschedules (
    id               TEXT PRIMARY KEY,
    action_item_id   TEXT NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    from_version     INTEGER NOT NULL,
    to_version       INTEGER NOT NULL,
    old_due_at       TIMESTAMPTZ NOT NULL,
    new_due_at       TIMESTAMPTZ NOT NULL,
    actor_id         TEXT NOT NULL REFERENCES users(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id       TEXT NOT NULL UNIQUE
);

-- One reminder dispatch per (action item, schedule version). The unique
-- constraint guarantees a due version reminds exactly once, even when
-- multiple scheduler ticks race.
CREATE TABLE reminder_dispatches (
    action_item_id TEXT NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    notified_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (action_item_id, version)
);

CREATE TABLE notifications (
    id             TEXT PRIMARY KEY,
    action_item_id TEXT NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    owner_id       TEXT NOT NULL REFERENCES users(id),
    message        TEXT NOT NULL,
    due_at         TIMESTAMPTZ NOT NULL,
    version        INTEGER NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_notifications_owner ON notifications(owner_id, created_at);

-- Idempotency / replay store for every mutating request. The response body
-- and status captured at first execution are returned verbatim on replay.
CREATE TABLE idempotent_requests (
    request_id   TEXT PRIMARY KEY,
    actor_id     TEXT NOT NULL,
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    response     JSONB NOT NULL,
    status_code  INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    id          TEXT PRIMARY KEY,
    incident_id TEXT REFERENCES incidents(id) ON DELETE CASCADE,
    actor_id    TEXT NOT NULL REFERENCES users(id),
    action      TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_incident ON audit_events(incident_id, created_at);
