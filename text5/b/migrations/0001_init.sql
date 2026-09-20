-- DeskLens initial schema.
-- Raw per-minute activity snapshots + versioned policy/classification metadata
-- + pre-aggregated daily/weekly summaries + retention bookkeeping.

CREATE TABLE departments (
    id   SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE employees (
    id            SERIAL PRIMARY KEY,
    department_id INT  NOT NULL REFERENCES departments (id),
    name          TEXT NOT NULL,
    timezone      TEXT NOT NULL -- IANA name, e.g. 'Asia/Shanghai'
);

CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    token         TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'ingest')),
    department_id INT REFERENCES departments (id) -- set for managers
);

-- Monitoring/privacy policy. Immutable once published; ingestion always
-- resolves the latest version and stamps it onto every raw row.
CREATE TABLE policy_versions (
    version                SERIAL PRIMARY KEY,
    work_start_minutes     INT       NOT NULL CHECK (work_start_minutes >= 0 AND work_start_minutes < 1440),
    work_end_minutes       INT       NOT NULL CHECK (work_end_minutes > 0 AND work_end_minutes <= 1440),
    workdays               SMALLINT[] NOT NULL, -- 0=Sunday .. 6=Saturday
    excluded_apps          TEXT[]    NOT NULL DEFAULT '{}',
    exempt_department_ids  INT[]     NOT NULL DEFAULT '{}',
    published_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (work_start_minutes < work_end_minutes)
);

CREATE TABLE classification_versions (
    version      SERIAL PRIMARY KEY,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Wildcard rules (* and ?), matched case-insensitively against the app name.
-- Highest priority wins; ties break by smallest rule id (stable).
CREATE TABLE classification_rules (
    id       BIGSERIAL PRIMARY KEY,
    version  INT  NOT NULL REFERENCES classification_versions (version),
    pattern  TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('productive', 'unproductive', 'neutral')),
    priority INT  NOT NULL DEFAULT 0
);

CREATE TABLE raw_snapshots (
    id                    BIGSERIAL PRIMARY KEY,
    workstation_id        TEXT        NOT NULL,
    employee_id           INT         NOT NULL REFERENCES employees (id),
    minute_utc            TIMESTAMPTZ NOT NULL,
    app_name              TEXT        NOT NULL,
    activity_count        BIGINT      NOT NULL CHECK (activity_count >= 0),
    content_hash          TEXT        NOT NULL, -- idempotency fingerprint of the payload
    category              TEXT        NOT NULL CHECK (category IN ('productive', 'unproductive', 'neutral')),
    policy_version        INT         NOT NULL,
    classification_version INT        NOT NULL,
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workstation_id, minute_utc) -- idempotency key
);

CREATE INDEX idx_raw_emp_minute ON raw_snapshots (employee_id, minute_utc);

CREATE TABLE employee_daily_summary (
    employee_id  INT  NOT NULL REFERENCES employees (id),
    day          DATE NOT NULL, -- employee-local calendar day
    productive   BIGINT NOT NULL DEFAULT 0,
    unproductive BIGINT NOT NULL DEFAULT 0,
    neutral      BIGINT NOT NULL DEFAULT 0,
    total        BIGINT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, day)
);

CREATE TABLE department_weekly_summary (
    department_id INT  NOT NULL REFERENCES departments (id),
    week_start    DATE NOT NULL, -- Monday of the ISO week
    productive    BIGINT NOT NULL DEFAULT 0,
    unproductive  BIGINT NOT NULL DEFAULT 0,
    neutral       BIGINT NOT NULL DEFAULT 0,
    total         BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (department_id, week_start)
);

-- Singleton: how far raw data has been cleaned up. Summaries for periods at
-- or after raw_cutoff can be rebuilt from raw_snapshots; earlier ones cannot.
CREATE TABLE retention_state (
    id         BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    raw_cutoff TIMESTAMPTZ
);

INSERT INTO retention_state (id, raw_cutoff) VALUES (TRUE, NULL);
