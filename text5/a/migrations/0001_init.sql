-- 0001_init.sql — DeskLens core schema.
--
-- Privacy model: filtered data (exempt departments, excluded apps, outside
-- the monitoring window) never reaches raw_snapshots, so no downstream table
-- can ever see it. Policy and classification versions are stamped on every
-- raw row so historical data is never silently reinterpreted.

CREATE TABLE schema_migrations (
    version     INT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE departments (
    id      BIGSERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    exempt  BOOLEAN NOT NULL DEFAULT FALSE   -- exempt departments are never monitored
);

CREATE TABLE employees (
    id            BIGSERIAL PRIMARY KEY,
    department_id BIGINT NOT NULL REFERENCES departments(id),
    name          TEXT NOT NULL,
    timezone      TEXT NOT NULL DEFAULT 'UTC',  -- IANA name, drives window check + local_date
    role          TEXT NOT NULL DEFAULT 'employee'
                  CHECK (role IN ('employee', 'manager', 'admin'))
);

CREATE TABLE workstations (
    id          TEXT PRIMARY KEY,             -- agent-supplied stable workstation id
    employee_id BIGINT NOT NULL REFERENCES employees(id)
);

-- Monitoring policy. A new row = a new version; only the max version is
-- applied to NEW ingestion. Existing raw rows keep the version they used.
CREATE TABLE policies (
    version        INT PRIMARY KEY,
    published_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    work_start     TIME NOT NULL,             -- local-time monitoring window [start, end)
    work_end       TIME NOT NULL,
    excluded_apps  JSONB NOT NULL DEFAULT '[]'  -- wildcard patterns, e.g. ["*game*"]
);

-- Classification rules, grouped by version. A version is published as a set;
-- rules are never edited in place, so old raw rows keep their classification.
CREATE TABLE classification_rules (
    id        BIGSERIAL PRIMARY KEY,
    version   INT NOT NULL,
    pattern   TEXT NOT NULL,                 -- case-insensitive wildcard: * and ?
    category  TEXT NOT NULL CHECK (category IN ('productive', 'unproductive', 'neutral')),
    priority  INT NOT NULL DEFAULT 0
);
CREATE INDEX idx_classification_rules_version ON classification_rules (version);

-- Raw per-minute activity snapshots. Idempotency key: (workstation_id, minute_utc).
CREATE TABLE raw_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    workstation_id  TEXT NOT NULL REFERENCES workstations(id),
    employee_id     BIGINT NOT NULL REFERENCES employees(id),
    minute_utc      TIMESTAMPTZ NOT NULL,
    app_name        TEXT NOT NULL,
    activity_count  INT NOT NULL CHECK (activity_count >= 0),
    local_date      DATE NOT NULL,           -- date in the employee's timezone
    category        TEXT NOT NULL CHECK (category IN ('productive', 'unproductive', 'neutral')),
    policy_version  INT NOT NULL,            -- policy actually applied at ingest
    rules_version   INT NOT NULL,            -- classification rules actually applied
    content_hash    BYTEA NOT NULL,          -- detects conflicting re-submissions
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workstation_id, minute_utc)
);
CREATE INDEX idx_raw_employee_date ON raw_snapshots (employee_id, local_date);
CREATE INDEX idx_raw_local_date ON raw_snapshots (local_date);

CREATE TABLE daily_summaries (
    employee_id        BIGINT NOT NULL REFERENCES employees(id),
    day                DATE NOT NULL,
    productive_count   BIGINT NOT NULL,
    unproductive_count BIGINT NOT NULL,
    neutral_count      BIGINT NOT NULL,
    total_count        BIGINT NOT NULL,
    snapshots          INT NOT NULL,
    rebuilt_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, day)
);

CREATE TABLE weekly_summaries (
    department_id      BIGINT NOT NULL REFERENCES departments(id),
    week_start         DATE NOT NULL,        -- ISO Monday
    productive_count   BIGINT NOT NULL,
    unproductive_count BIGINT NOT NULL,
    neutral_count      BIGINT NOT NULL,
    total_count        BIGINT NOT NULL,
    employees          INT NOT NULL,
    rebuilt_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (department_id, week_start)
);

-- Singleton bookkeeping row. raw_retained_from is the earliest local_date
-- whose raw data is still complete; rebuilds reaching before it are refused
-- so partial history can never overwrite complete summaries.
CREATE TABLE system_state (
    id                 BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    raw_retained_from  DATE
);
INSERT INTO system_state (id, raw_retained_from) VALUES (TRUE, NULL);

CREATE TABLE api_tokens (
    token       TEXT PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES employees(id)
);

-- Default policy v1: monitor 09:00-18:00 local, nothing excluded.
INSERT INTO policies (version, work_start, work_end, excluded_apps)
VALUES (1, '09:00', '18:00', '[]');

-- Default classification rules v1.
INSERT INTO classification_rules (version, pattern, category, priority) VALUES
    (1, '*slack*',        'unproductive', 10),
    (1, '*wechat*',       'unproductive', 10),
    (1, '*youtube*',      'unproductive', 10),
    (1, '*game*',         'unproductive', 10),
    (1, '*code*',         'productive',   10),
    (1, '*terminal*',     'productive',   10),
    (1, '*vim*',          'productive',   10),
    (1, '*excel*',        'productive',   10),
    (1, '*outlook*',      'neutral',      10);
