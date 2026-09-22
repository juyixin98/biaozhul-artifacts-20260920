-- DAMS schema: detection rules (versioned) and detection state
CREATE TABLE rule_versions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id              BIGINT NOT NULL REFERENCES organizations(id),
    rule_type           TEXT NOT NULL CHECK (rule_type IN ('frequency','sensitive_hours')),
    version             INT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    -- frequency:    {"window_seconds":300,"threshold":500,"actions":["select",...]}
    -- sensitive:    {"sensitive_tables":[{"schema":"public","table":"salaries"}],
    --                "allowed_start_hour":6,"allowed_end_hour":20,"actions":["select"]}
    params              JSONB NOT NULL,
    effective_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          BIGINT REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, rule_type, version)
);
CREATE INDEX idx_rules_active ON rule_versions(org_id, rule_type, is_active) WHERE is_active;
-- At most one active version per org + rule type. Deactivation + insert run in
-- the same transaction, so creating a new version can never leave two active.
CREATE UNIQUE INDEX uq_rule_one_active ON rule_versions(org_id, rule_type) WHERE is_active;

-- Frequency rule state: one row per (user, rule version, stepped window start).
CREATE TABLE detection_windows (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id              BIGINT NOT NULL REFERENCES organizations(id),
    rule_version_id     BIGINT NOT NULL REFERENCES rule_versions(id),
    db_user             TEXT NOT NULL,
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    event_count         INT NOT NULL DEFAULT 0,
    first_event_at      TIMESTAMPTZ,
    last_event_at       TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, rule_version_id, db_user, window_start)
);

-- Alerts are idempotent per fingerprint:
--   frequency:        rule_version_id | db_user | window_start
--   sensitive_hours:  rule_version_id | event_id
CREATE TABLE alerts (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id              BIGINT NOT NULL REFERENCES organizations(id),
    rule_version_id     BIGINT NOT NULL REFERENCES rule_versions(id),
    rule_type           TEXT NOT NULL,
    fingerprint         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','investigating','resolved','false_positive')),
    -- optimistic concurrency token for investigator transitions
    version             INT NOT NULL DEFAULT 1,
    title               TEXT NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by         BIGINT REFERENCES users(id),
    UNIQUE (org_id, fingerprint)
);
CREATE INDEX idx_alerts_org_status ON alerts(org_id, status);

-- Events contributing to an alert (frequency: every event in the offending window).
CREATE TABLE alert_events (
    alert_id            BIGINT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    event_id            BIGINT NOT NULL REFERENCES events(id),
    added_by_revision   INT NOT NULL DEFAULT 1,
    PRIMARY KEY (alert_id, event_id)
);

-- Append-only investigator lifecycle log.
CREATE TABLE alert_status_history (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alert_id            BIGINT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    from_status         TEXT,
    to_status           TEXT NOT NULL,
    note                TEXT NOT NULL DEFAULT '',
    acted_by            BIGINT REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only machine evidence. Recomputation may ADD revisions but never
-- overwrite an investigator decision. seq is unique per alert.
CREATE TABLE alert_evidence_revisions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alert_id            BIGINT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    seq                 INT NOT NULL,
    event_count         INT NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (alert_id, seq)
);
