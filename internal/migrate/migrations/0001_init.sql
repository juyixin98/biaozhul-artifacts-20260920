-- SIRCC schema: incident lifecycle, idempotent transitions, evidence, action items, reminders.

CREATE TABLE users (
    id         uuid PRIMARY KEY,
    username   text NOT NULL UNIQUE,
    role       text NOT NULL CHECK (role IN ('analyst', 'responder', 'admin')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE incidents (
    id              uuid PRIMARY KEY,
    title           text NOT NULL,
    description     text NOT NULL DEFAULT '',
    severity        text NOT NULL CHECK (severity IN ('P1', 'P2', 'P3', 'P4')),
    status          text NOT NULL DEFAULT 'detected'
                    CHECK (status IN ('detected', 'triaged', 'contained', 'eradicated',
                                      'recovered', 'postmortem', 'closed')),
    version         bigint NOT NULL DEFAULT 1,
    assignee_id     uuid REFERENCES users (id),
    root_cause      text NOT NULL DEFAULT '',
    lessons_learned text NOT NULL DEFAULT '',
    created_by      uuid NOT NULL REFERENCES users (id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One row per accepted transition. (incident_id, request_id) enforces idempotency:
-- a replayed request finds its original result here instead of re-applying.
CREATE TABLE phase_transitions (
    id            uuid PRIMARY KEY,
    incident_id   uuid NOT NULL REFERENCES incidents (id),
    request_id    text NOT NULL,
    from_status   text NOT NULL,
    to_status     text NOT NULL,
    actor_id      uuid NOT NULL REFERENCES users (id),
    note          text NOT NULL DEFAULT '',
    version_after bigint NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (incident_id, request_id)
);

CREATE TABLE audit_events (
    id          uuid PRIMARY KEY,
    incident_id uuid NOT NULL REFERENCES incidents (id),
    actor_id    uuid REFERENCES users (id),
    event_type  text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evidence (
    id          uuid PRIMARY KEY,
    incident_id uuid NOT NULL REFERENCES incidents (id),
    seq         integer NOT NULL,
    content     text NOT NULL,
    author_id   uuid NOT NULL REFERENCES users (id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (incident_id, seq)
);

-- Evidence is immutable once submitted; corrections are appended as linked notes.
CREATE TABLE evidence_notes (
    id          uuid PRIMARY KEY,
    evidence_id uuid NOT NULL REFERENCES evidence (id),
    content     text NOT NULL,
    author_id   uuid NOT NULL REFERENCES users (id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE action_items (
    id                    uuid PRIMARY KEY,
    incident_id           uuid NOT NULL REFERENCES incidents (id),
    title                 text NOT NULL,
    owner_id              uuid NOT NULL REFERENCES users (id),
    due_at                timestamptz NOT NULL,
    status                text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    -- due_version increments on every reschedule; reminders fire at most once per version.
    due_version           bigint NOT NULL DEFAULT 1,
    reminded_due_version  bigint NOT NULL DEFAULT 0,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- Persistent reminder log; the unique key makes duplicate delivery impossible.
CREATE TABLE reminders (
    id             uuid PRIMARY KEY,
    action_item_id uuid NOT NULL REFERENCES action_items (id),
    due_version    bigint NOT NULL,
    sent_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (action_item_id, due_version)
);

-- Seed users used by samples, docs and tests. No external identity provider is used;
-- callers authenticate with the X-User-Id header.
INSERT INTO users (id, username, role) VALUES
    ('00000000-0000-0000-0000-0000000000a1', 'admin1', 'admin'),
    ('00000000-0000-0000-0000-0000000000a2', 'analyst1', 'analyst'),
    ('00000000-0000-0000-0000-0000000000a3', 'analyst2', 'analyst'),
    ('00000000-0000-0000-0000-0000000000b1', 'responder1', 'responder'),
    ('00000000-0000-0000-0000-0000000000b2', 'responder2', 'responder');
