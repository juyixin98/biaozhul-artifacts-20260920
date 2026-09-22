-- DAMS initial schema.
-- All timestamps are timestamptz; window boundaries for rate rules are aligned
-- to UTC epoch (see detection package). Business hours for sensitive-table
-- rules are interpreted in the organization's IANA time zone.

CREATE TABLE organizations (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL DEFAULT 'UTC',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A user may belong to several organizations with one role each.
-- Roles: admin (configure rules, ingest), analyst (work assigned alerts),
-- auditor (read-only everywhere).
CREATE TABLE memberships (
    org_id   BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role     TEXT NOT NULL CHECK (role IN ('admin','analyst','auditor')),
    PRIMARY KEY (org_id, user_id)
);

-- Local-test-only bearer tokens. We store sha256(token), never the token
-- itself. Demo tokens are printed once by 'damsctl seed'.
CREATE TABLE api_tokens (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL DEFAULT 'default',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sources (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source_key  TEXT NOT NULL,           -- logical source database identity
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, source_key)
);

-- Rule configuration. Editing a rule INSERTs a new version row; the highest
-- version is current. The rule id comes from a global sequence so all
-- versions of the same rule share an id, while (id, version) is the PK.
CREATE SEQUENCE rules_id_seq;

CREATE TABLE rules (
    id              BIGINT NOT NULL DEFAULT nextval('rules_id_seq'),
    org_id          BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('rate','sensitive')),
    name            TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    -- rate rule params
    window_seconds  INTEGER NOT NULL DEFAULT 300,
    max_events      INTEGER NOT NULL DEFAULT 500,
    -- sensitive rule params
    tables          TEXT[]  NOT NULL DEFAULT '{}',  -- lower-case, schema.table or table
    hour_start      INTEGER NOT NULL DEFAULT 6,      -- allowed hour, inclusive
    hour_end        INTEGER NOT NULL DEFAULT 20,     -- closing boundary, exclusive
    config_json     JSONB   NOT NULL DEFAULT '{}',
    created_by      BIGINT REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, version)
);
CREATE INDEX idx_rules_current ON rules (org_id, id, version DESC);

CREATE TABLE events (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source_id     BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_key    TEXT NOT NULL,
    event_id      TEXT NOT NULL,           -- producer-assigned id, unique per source
    db_user       TEXT NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL,
    action        TEXT NOT NULL,           -- select/insert/update/delete/ddl/...
    schema_name   TEXT NOT NULL DEFAULT '',
    table_name    TEXT NOT NULL DEFAULT '',
    row_count     BIGINT NOT NULL DEFAULT 0,
    -- Fingerprint of the event payload used to detect same-id/different-content.
    content_hash  TEXT NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, event_id),
    -- source_key is denormalized so the dedup index matches the API contract
    -- ("source + event id"); source_id already enforces it, this index backs
    -- cross-source lookups of conflicts within a batch.
    UNIQUE (org_id, source_key, event_id)
);
CREATE INDEX idx_events_user_time ON events (org_id, db_user, occurred_at);
CREATE INDEX idx_events_occurred ON events (org_id, occurred_at);

CREATE TABLE alerts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    rule_id       BIGINT NOT NULL,
    rule_version  INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('rate','sensitive')),
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','investigating','resolved','false_positive')),
    -- Stable identity so recomputation never produces a duplicate alert:
    -- rate:      "rate:<ruleId>:v<version>:<dbUser>:<windowStartUnix>"
    -- sensitive: "sensitive:<ruleId>:v<version>:<eventId>"
    fingerprint   TEXT NOT NULL,
    window_start  TIMESTAMPTZ,             -- rate alerts only
    window_end    TIMESTAMPTZ,             -- rate alerts only
    db_user       TEXT,                    -- rate alerts only
    event_id      TEXT,                    -- sensitive alerts only (producer id)
    event_pk      BIGINT REFERENCES events(id),
    event_count   INTEGER NOT NULL DEFAULT 0,
    assigned_to   BIGINT REFERENCES users(id),
    -- Optimistic-concurrency token; incremented on every state change.
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, fingerprint)
);
CREATE INDEX idx_alerts_org_status ON alerts (org_id, status);

-- Many-to-many evidence. A rate alert links every event counted in the
-- window; late events are INSERTed here on recompute (dedup by PK pair).
CREATE TABLE alert_events (
    alert_id  BIGINT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    event_pk  BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY (alert_id, event_pk)
);

-- Append-only alert history. Recomputation may append 'recompute' evidence
-- revisions but can never overwrite an investigator's 'transition' decision.
CREATE TABLE alert_revisions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alert_id    BIGINT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    revision    TEXT NOT NULL CHECK (revision IN
                ('created','transition','note','assign','recompute')),
    from_status TEXT,
    to_status   TEXT,
    note        TEXT NOT NULL DEFAULT '',
    event_count INTEGER,
    actor_id    BIGINT REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_alert_revisions_alert ON alert_revisions (alert_id, id);

-- Append-only, hash-chained audit log. One independent chain per organization,
-- numbered from 1. Appends are serialized with a per-org advisory xact lock.
CREATE TABLE audit_entries (
    org_id        BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    seq           BIGINT NOT NULL,
    actor_id      BIGINT REFERENCES users(id),
    action        TEXT NOT NULL,
    content       JSONB NOT NULL,
    content_hash  TEXT NOT NULL,           -- sha256 of canonical JSON
    prev_hash     TEXT NOT NULL,           -- sha256 of prior entry_hash, or ''
    entry_hash    TEXT NOT NULL,           -- sha256(seq|org|action|content_hash|prev_hash)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, seq)
);

-- Which event columns are masked on export, per org (defaults applied in code:
-- db_user, table_name). Values are event column names.
CREATE TABLE export_policies (
    org_id        BIGINT PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    masked_fields TEXT[] NOT NULL DEFAULT '{db_user,table_name}',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Append-only enforcement. The audit chain and alert history must never be
-- updated or deleted through normal operation; only INSERT is allowed.
-- Legitimate integrity tooling can bypass this as the table owner with
-- SET session_replication_role = replica.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dams_deny_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only; % is forbidden',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER trg_audit_entries_immutable
    BEFORE UPDATE OR DELETE ON audit_entries
    FOR EACH ROW EXECUTE FUNCTION dams_deny_mutation();

CREATE TRIGGER TRG_ALERT_REVISIONS_IMMUTABLE
    BEFORE UPDATE OR DELETE ON alert_revisions
    FOR EACH ROW EXECUTE FUNCTION dams_deny_mutation();
