-- 0001_init.sql — artifact promotion atomicity schema
--
-- Invariants enforced here:
--   * environments.generation only moves forward, and only via the
--     optimistic-concurrency UPDATE in the pointer-commit transaction.
--   * env_history is an append-only log of every pointer value ever
--     committed (including the current one); rollback targets come from it.
--   * attempts records every promotion/rollback/ingest attempt with its
--     full step log; a 'running' attempt always means the pointer commit
--     never completed.

CREATE TABLE IF NOT EXISTS environments (
    name                   TEXT PRIMARY KEY,
    current_digest         TEXT,
    generation             BIGINT NOT NULL DEFAULT 0,
    retention_keep         INT  NOT NULL DEFAULT 5  CHECK (retention_keep >= 0),
    retention_max_age_days INT  NOT NULL DEFAULT 30 CHECK (retention_max_age_days >= 0),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artifacts (
    digest      TEXT PRIMARY KEY,            -- 'sha256:<64 hex>'
    size_bytes  BIGINT NOT NULL,
    media_type  TEXT NOT NULL DEFAULT 'application/octet-stream',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Test evidence, immutable and versioned per id.
CREATE TABLE IF NOT EXISTS evidence (
    id              TEXT NOT NULL,
    version         INT  NOT NULL,
    artifact_digest TEXT NOT NULL,
    suite           TEXT NOT NULL,
    passed          BOOLEAN NOT NULL,
    report          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, version)
);

-- Promotion policy, immutable and versioned per id.
CREATE TABLE IF NOT EXISTS policies (
    id                   TEXT NOT NULL,
    version              INT  NOT NULL,
    source_env           TEXT NOT NULL,
    target_env           TEXT NOT NULL,
    required_suite       TEXT NOT NULL,
    min_evidence_version INT  NOT NULL DEFAULT 1,
    body                 JSONB NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, version)
);

-- An approval is bound to the environment generation observed at issuance.
-- Once the environment moves past that generation the approval is 'late'
-- and can no longer authorize a promotion.
CREATE TABLE IF NOT EXISTS approvals (
    id              UUID PRIMARY KEY,
    environment     TEXT NOT NULL,
    generation      BIGINT NOT NULL,
    artifact_digest TEXT NOT NULL,
    approver        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS attempts (
    id                  UUID PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('ingest','promotion','rollback')),
    source_env          TEXT NOT NULL,
    target_env          TEXT NOT NULL,
    artifact_digest     TEXT NOT NULL,
    expected_generation BIGINT NOT NULL,
    policy_id           TEXT,
    policy_version      INT,
    evidence_id         TEXT,
    evidence_version    INT,
    approval_id         UUID,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','succeeded','failed','interrupted')),
    failure_reason      TEXT,
    steps               JSONB NOT NULL DEFAULT '[]',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS attempts_status_idx ON attempts (status);
CREATE INDEX IF NOT EXISTS attempts_target_idx  ON attempts (target_env);

CREATE TABLE IF NOT EXISTS env_history (
    environment TEXT NOT NULL,
    generation  BIGINT NOT NULL,
    digest      TEXT NOT NULL,
    attempt_id  UUID NOT NULL REFERENCES attempts (id),
    replaced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (environment, generation)
);
