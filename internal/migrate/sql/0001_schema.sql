-- CostLens schema: organizations, cost centers, accounts, users with role-based
-- grants, imported billing lines, summaries, budgets, anomalies and alerts.
-- All monetary values are NUMERIC (never float). Different currencies are never
-- mixed: every amount-bearing table carries a currency column and every unique
-- key / summary key includes it.

CREATE TABLE organizations (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cost_centers (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id    BIGINT NOT NULL REFERENCES organizations(id),
    code      TEXT NOT NULL,
    name      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, code)
);

-- A billing account belongs to exactly one organization and one cost center.
CREATE TABLE accounts (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id       BIGINT NOT NULL REFERENCES organizations(id),
    cost_center_id BIGINT NOT NULL REFERENCES cost_centers(id),
    external_id  TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX accounts_org_idx ON accounts(org_id);
CREATE INDEX accounts_cc_idx ON accounts(cost_center_id);

CREATE TABLE users (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE,
    -- admin: full access; analyst: import within granted orgs; viewer: read-only.
    role         TEXT NOT NULL CHECK (role IN ('admin','analyst','viewer')),
    api_token    TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_org_grants (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id  BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, org_id)
);

CREATE TABLE import_batches (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES accounts(id),
    filename    TEXT NOT NULL,
    total_rows  INT NOT NULL,
    inserted_rows INT NOT NULL,
    duplicate_rows INT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('completed','rolled_back','rebuild')),
    created_by  BIGINT REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One line per (account, resource, usage_date). content_hash covers every
-- payload column; an existing row with a different hash is a conflict and the
-- whole batch must roll back.
CREATE TABLE billing_records (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES accounts(id),
    resource_id  TEXT NOT NULL,
    service      TEXT NOT NULL,
    usage_date   DATE NOT NULL,
    currency     CHAR(3) NOT NULL,
    amount       NUMERIC(20,6) NOT NULL,
    content_hash TEXT NOT NULL,
    batch_id     BIGINT NOT NULL REFERENCES import_batches(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, resource_id, usage_date)
);
CREATE INDEX billing_records_account_date_idx ON billing_records(account_id, usage_date);
CREATE INDEX billing_records_cc_date_idx
    ON billing_records (usage_date) INCLUDE (amount, currency);

-- Daily/monthly summaries at two scopes: account and cost center.
-- Granularity is 'day'|'month'; month rows store usage_date = first of month.
-- The non-applicable scope column is 0 (never NULL): Postgres treats NULLs as
-- distinct in unique indexes, which would break ON CONFLICT upserts.
CREATE TABLE daily_summaries (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope        TEXT NOT NULL CHECK (scope IN ('account','cost_center')),
    account_id   BIGINT NOT NULL DEFAULT 0,
    cost_center_id BIGINT NOT NULL DEFAULT 0,
    usage_date   DATE NOT NULL,
    currency     CHAR(3) NOT NULL,
    total_amount NUMERIC(20,6) NOT NULL,
    record_count INT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((scope='account' AND account_id <> 0 AND cost_center_id = 0)
        OR (scope='cost_center' AND account_id = 0 AND cost_center_id <> 0)),
    UNIQUE (scope, account_id, cost_center_id, usage_date, currency)
);
CREATE INDEX daily_summaries_cc_month_idx
    ON daily_summaries(cost_center_id, usage_date) WHERE scope = 'cost_center';

CREATE TABLE monthly_summaries (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope        TEXT NOT NULL CHECK (scope IN ('account','cost_center')),
    account_id   BIGINT NOT NULL DEFAULT 0,
    cost_center_id BIGINT NOT NULL DEFAULT 0,
    month        DATE NOT NULL CHECK (month = date_trunc('month', month)::date),
    currency     CHAR(3) NOT NULL,
    total_amount NUMERIC(20,6) NOT NULL,
    record_count INT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((scope='account' AND account_id <> 0 AND cost_center_id = 0)
        OR (scope='cost_center' AND account_id = 0 AND cost_center_id <> 0)),
    UNIQUE (scope, account_id, cost_center_id, month, currency)
);

-- Budgets are versioned: a new limit creates a new immutable version; alerts
-- always reference the version that produced them, preserving history.
CREATE TABLE budgets (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cost_center_id BIGINT NOT NULL REFERENCES cost_centers(id),
    currency       CHAR(3) NOT NULL,
    month          DATE NOT NULL CHECK (month = date_trunc('month', month)::date),
    version        INT NOT NULL,
    monthly_limit  NUMERIC(20,6) NOT NULL CHECK (monthly_limit > 0),
    active         BOOLEAN NOT NULL DEFAULT true,
    created_by     BIGINT REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cost_center_id, currency, month, version)
);
-- Only one active version per (cc, currency, month).
CREATE UNIQUE INDEX budgets_active_one_idx
    ON budgets(cost_center_id, currency, month) WHERE active;

CREATE TABLE budget_alerts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    budget_id       BIGINT NOT NULL REFERENCES budgets(id),
    threshold_pct   INT NOT NULL CHECK (threshold_pct IN (50,75,90,100)),
    spent_amount    NUMERIC(20,6) NOT NULL,
    month           DATE NOT NULL,
    currency        CHAR(3) NOT NULL,
    triggered_by_batch BIGINT REFERENCES import_batches(id),
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A threshold fires at most once per budget version.
    UNIQUE (budget_id, threshold_pct)
);
CREATE INDEX budget_alerts_budget_idx ON budget_alerts(budget_id);

-- Anomaly evaluation is append-only with versioning. Late data that shifts a
-- baseline produces a new version for that (account, date, currency); prior
-- alerts and versions stay untouched.
CREATE TABLE anomaly_evaluations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES accounts(id),
    usage_date   DATE NOT NULL,
    currency     CHAR(3) NOT NULL,
    version      INT NOT NULL,
    -- anomalous | normal | insufficient_history | zero_variance_below
    status       TEXT NOT NULL CHECK (status IN
                    ('anomalous','normal','insufficient_history','zero_variance_below')),
    actual_amount NUMERIC(20,6) NOT NULL,
    baseline_mean NUMERIC(20,8),
    baseline_std  NUMERIC(20,8),
    threshold_amount NUMERIC(20,8),
    baseline_start DATE,
    baseline_end   DATE,
    baseline_days  INT NOT NULL DEFAULT 0,
    created_by_batch BIGINT REFERENCES import_batches(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, usage_date, currency, version)
);
CREATE INDEX anomaly_evals_lookup ON anomaly_evaluations(account_id, usage_date, currency);

CREATE TABLE rebuild_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
    started_by  BIGINT REFERENCES users(id),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
