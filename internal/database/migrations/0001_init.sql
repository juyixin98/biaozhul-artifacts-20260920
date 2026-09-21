-- DeskLens schema.
-- All timestamps are timestamptz (UTC); local_date/local_week are computed in
-- the employee's configured IANA timezone so cross-day boundaries are stable.

CREATE TABLE departments (
    id          BIGINT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    exempt      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE employees (
    id              BIGINT PRIMARY KEY,
    department_id   BIGINT NOT NULL REFERENCES departments(id),
    full_name       TEXT NOT NULL,
    timezone        TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX idx_employees_department ON employees(department_id);

CREATE TABLE workstations (
    id              BIGINT PRIMARY KEY,
    employee_id     BIGINT NOT NULL REFERENCES employees(id),
    label           TEXT NOT NULL
);

CREATE INDEX idx_workstations_employee ON workstations(employee_id);

-- ---------------------------------------------------------------------------
-- Monitoring policies. Only the highest published version is current.
-- Publishing a version never rewrites historical rows: every raw snapshot
-- records the exact policy_version applied at ingestion time.
-- ---------------------------------------------------------------------------
CREATE TABLE policy_versions (
    version             INTEGER PRIMARY KEY,
    window_start_minute INTEGER NOT NULL CHECK (window_start_minute BETWEEN 0 AND 1380),
    window_end_minute   INTEGER NOT NULL CHECK (window_end_minute BETWEEN 0 AND 1380),
    published_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE policy_excluded_apps (
    policy_version  INTEGER NOT NULL REFERENCES policy_versions(version) ON DELETE CASCADE,
    pattern         TEXT NOT NULL,
    PRIMARY KEY (policy_version, pattern)
);

CREATE TABLE policy_exempt_departments (
    policy_version  INTEGER NOT NULL REFERENCES policy_versions(version) ON DELETE CASCADE,
    department_id   BIGINT NOT NULL REFERENCES departments(id),
    PRIMARY KEY (policy_version, department_id)
);

-- ---------------------------------------------------------------------------
-- Application classification. Rules are matched by glob (case-insensitive),
-- the highest priority wins and ties break by rule_id. Every raw snapshot
-- stores both the category and the rule set version so rule edits never
-- silently recolor history.
-- ---------------------------------------------------------------------------
CREATE TABLE classification_versions (
    version     INTEGER PRIMARY KEY,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE classification_rules (
    version     INTEGER NOT NULL REFERENCES classification_versions(version) ON DELETE CASCADE,
    rule_id     BIGINT NOT NULL,
    pattern     TEXT NOT NULL,
    category    TEXT NOT NULL CHECK (category IN ('productive','non_productive','neutral')),
    priority    INTEGER NOT NULL,
    PRIMARY KEY (version, rule_id)
);

CREATE INDEX idx_classification_rules_lookup ON classification_rules(version, priority DESC, rule_id);

-- ---------------------------------------------------------------------------
-- Raw per-minute activity. Idempotency key = (workstation_id, minute_utc,
-- app_name). An identical repeat is dropped; a repeat that carries a
-- different activity_count is a hard conflict and aborts the whole batch.
-- Rows for exempt departments / excluded apps never reach this table.
-- ---------------------------------------------------------------------------
CREATE TABLE activity_snapshots (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workstation_id      BIGINT NOT NULL REFERENCES workstations(id),
    employee_id         BIGINT NOT NULL REFERENCES employees(id),
    department_id       BIGINT NOT NULL REFERENCES departments(id),
    minute_utc          TIMESTAMPTZ NOT NULL,
    app_name            TEXT NOT NULL,
    activity_count      INTEGER NOT NULL CHECK (activity_count >= 0),
    policy_version      INTEGER NOT NULL REFERENCES policy_versions(version),
    classif_version     INTEGER NOT NULL REFERENCES classification_versions(version),
    category            TEXT NOT NULL CHECK (category IN ('productive','non_productive','neutral')),
    local_date          DATE NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workstation_id, minute_utc, app_name)
);

CREATE INDEX idx_snapshots_emp_day ON activity_snapshots(employee_id, local_date);
CREATE INDEX idx_snapshots_dept_day ON activity_snapshots(department_id, local_date);
CREATE INDEX idx_snapshots_received ON activity_snapshots(received_at);

-- ---------------------------------------------------------------------------
-- Summaries. Recomputed wholesale (SQL SUM ... GROUP BY) from raw rows,
-- guarded by per-key transaction-scoped advisory locks, so the incremental
-- path and a full rebuild use exactly the same formula.
-- ---------------------------------------------------------------------------
CREATE TABLE employee_daily_summary (
    employee_id             BIGINT NOT NULL REFERENCES employees(id),
    local_date              DATE NOT NULL,
    department_id           BIGINT NOT NULL REFERENCES departments(id),
    productive_count        BIGINT NOT NULL,
    non_productive_count    BIGINT NOT NULL,
    neutral_count           BIGINT NOT NULL,
    active_minutes          INTEGER NOT NULL,
    policy_version          INTEGER NOT NULL,
    classif_version         INTEGER NOT NULL,
    recomputed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, local_date)
);

CREATE TABLE department_weekly_summary (
    department_id           BIGINT NOT NULL REFERENCES departments(id),
    iso_week                DATE NOT NULL,
    productive_count        BIGINT NOT NULL,
    non_productive_count    BIGINT NOT NULL,
    neutral_count           BIGINT NOT NULL,
    active_employee_days    INTEGER NOT NULL,
    recomputed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (department_id, iso_week)
);

-- Raw-data coverage per employee/day. 'full' = all raw minutes still present;
-- 'partial' = raw minutes were purged. Rebuild refuses to overwrite a summary
-- keyed to a day/week that lacks complete raw coverage.
CREATE TABLE employee_day_coverage (
    employee_id     BIGINT NOT NULL REFERENCES employees(id),
    local_date      DATE NOT NULL,
    raw_state       TEXT NOT NULL CHECK (raw_state IN ('full','partial')),
    raw_minutes     INTEGER NOT NULL,
    purged_at       TIMESTAMPTZ,
    PRIMARY KEY (employee_id, local_date)
);

-- Ledger of purges; defines the window within which raw data (and therefore
-- rebuilding) still exists.
CREATE TABLE raw_retention_runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    older_than_utc  TIMESTAMPTZ NOT NULL,
    purged_rows     BIGINT NOT NULL,
    ran_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- API tokens. manager tokens are scoped to one department; admin tokens have
-- no scope and can publish policy/rules, rebuild and purge.
-- ---------------------------------------------------------------------------
CREATE TABLE api_tokens (
    token           TEXT PRIMARY KEY,
    role            TEXT NOT NULL CHECK (role IN ('admin','manager')),
    department_id   BIGINT REFERENCES departments(id),
    description     TEXT NOT NULL DEFAULT ''
);
