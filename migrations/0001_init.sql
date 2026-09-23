-- CostLens initial schema
-- All money values are exact fixed-point: NUMERIC(20,6) (20 significant digits, 6 decimal places).
-- Money must never be stored or computed as floating point.

CREATE TYPE role_kind AS ENUM ('admin', 'analyst', 'viewer');
CREATE TYPE import_status AS ENUM ('succeeded', 'failed');

CREATE TABLE organizations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code        text NOT NULL UNIQUE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cost_centers (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES organizations(id),
    code        text NOT NULL,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, code)
);

CREATE TABLE accounts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           uuid NOT NULL REFERENCES organizations(id),
    cost_center_id   uuid NOT NULL REFERENCES cost_centers(id),
    code             text NOT NULL,
    name             text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, code)
);
CREATE INDEX idx_accounts_cost_center ON accounts(cost_center_id);

CREATE TABLE resources (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code        text NOT NULL UNIQUE,
    service     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_users (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    login       text NOT NULL UNIQUE,
    full_name   text NOT NULL,
    role        role_kind NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE user_org_scopes (
    user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    org_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, org_id)
);

CREATE TABLE api_keys (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    key_hash    text NOT NULL UNIQUE,
    hint        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);
CREATE INDEX idx_api_keys_user ON api_keys(user_id) WHERE revoked_at IS NULL;

CREATE TABLE cost_imports (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid REFERENCES organizations(id),
    user_id       uuid REFERENCES app_users(id),
    filename      text NOT NULL,
    status        import_status NOT NULL,
    raw_row_count integer NOT NULL DEFAULT 0,  -- data rows seen in the CSV
    row_count     integer NOT NULL DEFAULT 0,  -- new cost rows actually inserted
    error_line    integer,
    error_code    text,
    error_message text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_cost_imports_org ON cost_imports(org_id, created_at DESC);

CREATE TABLE costs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id   uuid NOT NULL REFERENCES cost_imports(id),
    account_id  uuid NOT NULL REFERENCES accounts(id),
    resource_id uuid NOT NULL REFERENCES resources(id),
    service     text NOT NULL,
    cost_date   date NOT NULL,
    currency    char(3) NOT NULL,
    amount      numeric(20,6) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- Dedup key: same account + resource + day is one bill line.
    UNIQUE (account_id, resource_id, cost_date)
);
-- Defense-in-depth: service on a bill line must be the resource's known service.
CREATE OR REPLACE FUNCTION costs_service_matches_resource() RETURNS trigger AS $$
BEGIN
    IF NEW.service IS DISTINCT FROM (SELECT service FROM resources WHERE id = NEW.resource_id) THEN
        RAISE EXCEPTION 'service % does not match resource service', NEW.service
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_costs_service_check BEFORE INSERT OR UPDATE ON costs
    FOR EACH ROW EXECUTE FUNCTION costs_service_matches_resource();
CREATE INDEX idx_costs_date ON costs(cost_date);
CREATE INDEX idx_costs_account_date ON costs(account_id, cost_date);
CREATE INDEX idx_costs_resource ON costs(resource_id);

CREATE TABLE daily_account_summary (
    account_id  uuid NOT NULL REFERENCES accounts(id),
    currency    char(3) NOT NULL,
    cost_date   date NOT NULL,
    total       numeric(20,6) NOT NULL,
    row_count   integer NOT NULL,
    PRIMARY KEY (account_id, currency, cost_date)
);

CREATE TABLE monthly_account_summary (
    account_id  uuid NOT NULL REFERENCES accounts(id),
    currency    char(3) NOT NULL,
    period      date NOT NULL,  -- first day of month
    total       numeric(20,6) NOT NULL,
    row_count   integer NOT NULL,
    PRIMARY KEY (account_id, currency, period)
);

CREATE TABLE daily_cost_center_summary (
    cost_center_id uuid NOT NULL REFERENCES cost_centers(id),
    currency       char(3) NOT NULL,
    cost_date      date NOT NULL,
    total          numeric(20,6) NOT NULL,
    row_count      integer NOT NULL,
    PRIMARY KEY (cost_center_id, currency, cost_date)
);

CREATE TABLE monthly_cost_center_summary (
    cost_center_id uuid NOT NULL REFERENCES cost_centers(id),
    currency       char(3) NOT NULL,
    period         date NOT NULL,  -- first day of month
    total          numeric(20,6) NOT NULL,
    row_count      integer NOT NULL,
    PRIMARY KEY (cost_center_id, currency, period)
);

-- One run groups all anomaly evaluations produced by one import or one rebuild.
CREATE TABLE anomaly_runs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL CHECK (kind IN ('import','rebuild','manual')),
    import_id   uuid REFERENCES cost_imports(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- A budget is set per cost center + currency + month. Each change creates a new
-- immutable version; budget_alerts reference the version that justified them.
CREATE TABLE budget_versions (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cost_center_id uuid NOT NULL REFERENCES cost_centers(id),
    currency       char(3) NOT NULL,
    period         date NOT NULL,  -- first day of month
    amount         numeric(20,6) NOT NULL CHECK (amount >= 0),
    version        integer NOT NULL,
    created_by     uuid REFERENCES app_users(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (cost_center_id, currency, period, version)
);
CREATE INDEX idx_budget_versions_lookup ON budget_versions(cost_center_id, currency, period, version DESC);

CREATE TABLE budget_alerts (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    budget_version_id uuid NOT NULL REFERENCES budget_versions(id),
    cost_center_id    uuid NOT NULL REFERENCES cost_centers(id),
    currency          char(3) NOT NULL,
    period            date NOT NULL,
    threshold         numeric(9,6) NOT NULL CHECK (threshold > 0 AND threshold <= 1),
    actual_amount     numeric(20,6) NOT NULL,
    triggered_by_run  uuid REFERENCES anomaly_runs, -- nullable: import or rebuild path
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- Exactly one alert per budget version + threshold: no duplicate alerts.
    UNIQUE (budget_version_id, threshold)
);
CREATE INDEX idx_budget_alerts_cc ON budget_alerts(cost_center_id, currency, period);

-- Append-only: when late data changes a baseline, old evaluations/alerts stay
-- (historical basis) and a new evaluation with a later run is inserted.
CREATE TABLE anomaly_evaluations (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        uuid NOT NULL REFERENCES anomaly_runs(id),
    account_id    uuid NOT NULL REFERENCES accounts(id),
    currency      char(3) NOT NULL,
    cost_date     date NOT NULL,
    actual_amount numeric(20,6) NOT NULL,
    baseline_mean numeric(20,6),
    baseline_std  numeric(20,6),
    threshold     numeric(20,6),
    status        text NOT NULL CHECK (status IN ('anomaly','normal','insufficient_history')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, account_id, currency, cost_date)
);
CREATE INDEX idx_anomaly_eval_date ON anomaly_evaluations(cost_date);

CREATE VIEW latest_anomaly_alerts AS
SELECT DISTINCT ON (account_id, currency, cost_date)
       id, run_id, account_id, currency, cost_date,
       actual_amount, baseline_mean, baseline_std, threshold, created_at
FROM anomaly_evaluations
WHERE status = 'anomaly'
ORDER BY account_id, currency, cost_date, created_at DESC, id DESC;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
