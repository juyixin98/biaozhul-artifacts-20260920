-- SIRCC core schema: users, incidents, phases, audit, evidence, action items, idempotency.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username      text NOT NULL UNIQUE,
    full_name     text NOT NULL,
    role          text NOT NULL CHECK (role IN ('admin', 'analyst', 'responder')),
    api_key_hash  text NOT NULL UNIQUE,
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE incidents (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title            text NOT NULL CHECK (length(btrim(title)) > 0),
    description      text NOT NULL DEFAULT '',
    severity         text NOT NULL CHECK (severity IN ('P1', 'P2', 'P3', 'P4')),
    status           text NOT NULL DEFAULT 'detected'
                       CHECK (status IN ('detected', 'triaged', 'contained',
                                         'eradicated', 'recovered', 'reviewed', 'closed')),
    version          bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    root_cause       text,
    lessons_learned  text,
    detected_at      timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz,
    created_by       uuid NOT NULL REFERENCES users (id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_incidents_status ON incidents (status);

-- Case scope: only members of an incident (or admins) may act on it.
CREATE TABLE incident_members (
    incident_id  uuid NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    user_id      uuid NOT NULL REFERENCES users (id),
    case_role    text NOT NULL CHECK (case_role IN ('analyst', 'responder', 'admin')),
    assigned_by  uuid REFERENCES users (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (incident_id, user_id)
);

-- One row per phase actually entered. Duration metrics are derived ONLY from these rows.
CREATE TABLE incident_phases (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id  uuid NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    phase        text NOT NULL CHECK (phase IN ('detection', 'triage', 'containment',
                                                'eradication', 'recovery', 'review', 'closure')),
    actor_id     uuid NOT NULL REFERENCES users (id),
    request_id   uuid,
    entered_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (incident_id, phase)
);

CREATE TABLE audit_events (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id  uuid NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    actor_id     uuid NOT NULL REFERENCES users (id),
    action       text NOT NULL,
    from_status  text,
    to_status    text,
    request_id   uuid,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_incident ON audit_events (incident_id, created_at);

-- Append-only evidence. Corrections may only be attached as linked notes.
CREATE TABLE evidence (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id   uuid NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    seq           integer NOT NULL CHECK (seq BETWEEN 1 AND 50),
    content       text NOT NULL CHECK (length(content) BETWEEN 1 AND 100000),
    submitted_by  uuid NOT NULL REFERENCES users (id),
    request_id    uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (incident_id, seq)
);

CREATE TABLE evidence_notes (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_id  uuid NOT NULL REFERENCES evidence (id) ON DELETE CASCADE,
    incident_id  uuid NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    author_id    uuid NOT NULL REFERENCES users (id),
    note         text NOT NULL CHECK (length(btrim(note)) > 0 AND length(note) <= 100000),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_evidence_notes_evidence ON evidence_notes (evidence_id, created_at);

CREATE TABLE action_items (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id    uuid NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    description    text NOT NULL CHECK (length(btrim(description)) > 0 AND length(description) <= 10000),
    owner_user_id  uuid NOT NULL REFERENCES users (id),
    due_at         timestamptz NOT NULL,
    due_version    integer NOT NULL DEFAULT 1 CHECK (due_version > 0),
    status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'canceled')),
    created_by     uuid NOT NULL REFERENCES users (id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
-- Scheduler scans open items by due time.
CREATE INDEX idx_action_items_due ON action_items (due_at) WHERE status = 'open';

-- Stored idempotency records: duplicate request ids replay the original response.
CREATE TABLE idempotent_requests (
    request_id     uuid PRIMARY KEY,
    user_id        uuid NOT NULL,
    incident_id    uuid,
    method         text NOT NULL,
    path           text NOT NULL,
    status_code    integer NOT NULL,
    response_body  jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Demo users (API keys are demonstration only, hashed with sha256).
INSERT INTO users (id, username, full_name, role, api_key_hash) VALUES
    ('11111111-1111-1111-1111-111111111101', 'admin',      'Alice Admin',     'admin',
     encode(digest('sircc_demo_admin_0001', 'sha256'), 'hex')),
    ('11111111-1111-1111-1111-111111111201', 'analyst1',   'Ana Analyst',     'analyst',
     encode(digest('sircc_demo_analyst_0001', 'sha256'), 'hex')),
    ('11111111-1111-1111-1111-111111111202', 'analyst2',   'Ari Analyst',     'analyst',
     encode(digest('sircc_demo_analyst_0002', 'sha256'), 'hex')),
    ('11111111-1111-1111-1111-111111111301', 'responder1', 'Rex Responder',   'responder',
     encode(digest('sircc_demo_responder_0001', 'sha256'), 'hex')),
    ('11111111-1111-1111-1111-111111111302', 'responder2', 'Rita Responder',  'responder',
     encode(digest('sircc_demo_responder_0002', 'sha256'), 'hex'))
ON CONFLICT DO NOTHING;
