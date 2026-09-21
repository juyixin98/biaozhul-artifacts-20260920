CREATE TYPE severity_level AS ENUM ('P1', 'P2', 'P3', 'P4');
CREATE TYPE incident_status AS ENUM ('detected', 'triaged', 'contained', 'eradicated', 'recovered', 'postmortem', 'closed');
CREATE TYPE member_role AS ENUM ('analyst', 'responder');

CREATE TABLE incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    severity severity_level NOT NULL,
    status incident_status NOT NULL DEFAULT 'detected',
    version INTEGER NOT NULL DEFAULT 1,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    root_cause TEXT NOT NULL DEFAULT '',
    lessons_learned TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE incident_members (
    incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role member_role NOT NULL,
    assigned_by TEXT NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (incident_id, user_id, role)
);

CREATE TABLE phase_records (
    id BIGSERIAL PRIMARY KEY,
    incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    phase incident_status NOT NULL,
    entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    exited_at TIMESTAMPTZ
);
-- At most one open (not yet exited) phase per incident.
CREATE UNIQUE INDEX phase_records_one_open ON phase_records (incident_id) WHERE exited_at IS NULL;
CREATE INDEX phase_records_incident ON phase_records (incident_id);

CREATE TABLE audit_events (
    id BIGSERIAL PRIMARY KEY,
    incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status incident_status,
    to_status incident_status,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_incident ON audit_events (incident_id);

-- Idempotency ledger for state transitions: one row per request_id.
CREATE TABLE transition_requests (
    request_id TEXT PRIMARY KEY,
    incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    content TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (incident_id, seq)
);

-- Corrections to evidence are append-only notes linked to the original entry.
CREATE TABLE evidence_notes (
    id BIGSERIAL PRIMARY KEY,
    evidence_id UUID NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE action_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    -- bumped on every reschedule so stale schedules never fire
    due_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Persistent reminders: at most one per (action_item, due_version).
CREATE TABLE reminders (
    id BIGSERIAL PRIMARY KEY,
    action_item_id UUID NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    due_version INTEGER NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (action_item_id, due_version)
);
