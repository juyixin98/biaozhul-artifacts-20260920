-- DeskLens schema.
-- Design notes:
--   * activity_snapshots holds per-minute snapshots only. Excluded apps, exempt
--     departments and snapshots outside the monitoring window are filtered at
--     ingest time and NEVER reach this table (privacy filtering).
--   * every raw row records the policy_version and classification_version that
--     were active when it was accepted, so later publications cannot silently
--     rewrite history.
--   * summary tables are derivable from activity_snapshots with the exact same
--     recompute functions used by incremental ingest and full rebuilds.

create table departments (
    id          bigint primary key,
    name        text not null,
    created_at  timestamptz not null default now()
);

create table employees (
    id             bigint primary key,
    department_id  bigint not null references departments(id),
    display_name   text not null,
    timezone       text not null,
    created_at     timestamptz not null default now(),
    constraint employees_timezone_check check (position('/' in timezone) > 0)
);
create index idx_employees_department on employees(department_id);

create table workstations (
    id           text primary key,
    employee_id  bigint not null references employees(id),
    created_at   timestamptz not null default now()
);

-- Credentials are opaque random keys. A workstation key only authenticates its
-- own workstation; a manager key is bound to one department.
create table ingest_api_keys (
    key             text primary key,
    workstation_id  text not null references workstations(id),
    active          boolean not null default true,
    created_at      timestamptz not null default now()
);

create table manager_api_keys (
    key            text primary key,
    username       text not null,
    department_id  bigint not null references departments(id),
    active         boolean not null default true,
    created_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Policy versions (monitoring window, excluded applications, exempt depts).
-- Publishing inserts a new row; ingest always resolves "current = max(version)"
-- and stamps the version it actually used on every raw row.
-- ---------------------------------------------------------------------------

create table policy_versions (
    version                  int generated always as identity primary key,
    monitoring_start_minute  integer not null
        check (monitoring_start_minute between 0 and 1439),
    monitoring_end_minute    integer not null
        check (monitoring_end_minute between 1 and 1440),
    check (monitoring_start_minute <> monitoring_end_minute),
    published_at             timestamptz not null default now(),
    published_by             text not null default '',
    note                     text not null default ''
);

create table policy_excluded_apps (
    policy_version  integer not null
        references policy_versions(version) on delete cascade,
    position        integer not null,
    pattern         text not null,
    primary key (policy_version, position)
);

create table policy_exempt_departments (
    policy_version  integer not null
        references policy_versions(version) on delete cascade,
    department_id   bigint not null references departments(id),
    primary key (policy_version, department_id)
);

-- ---------------------------------------------------------------------------
-- Classification versions and rules.
-- Rules are evaluated in order (priority ASC, rule_id ASC); the first matching
-- rule wins, so ties on priority are resolved deterministically by rule_id.
-- ---------------------------------------------------------------------------

create table classification_versions (
    version       bigint generated always as identity primary key,
    published_at  timestamptz not null default now(),
    published_by  text not null default '',
    note          text not null default ''
);

create table classification_rules (
    classification_version  bigint not null
        references classification_versions(version) on delete cascade,
    rule_id                 text not null,
    pattern                 text not null,
    category                text not null
        check (category in ('productive', 'unproductive', 'neutral')),
    priority                integer not null check (priority >= 0),
    primary key (classification_version, rule_id)
);
create index idx_classification_rules_lookup
    on classification_rules(classification_version, priority, rule_id);

-- ---------------------------------------------------------------------------
-- Raw per-minute activity snapshots.
-- (workstation_id, bucket_time) is the idempotency key: an identical re-send
-- is a duplicate and is ignored; a re-send with different content conflicts and
-- the whole batch is rejected.
-- ---------------------------------------------------------------------------

create table activity_snapshots (
    workstation_id          text not null references workstations(id),
    employee_id             bigint not null references employees(id),
    bucket_time             timestamptz not null,
    app_name                text not null,
    activity_count          integer not null check (activity_count >= 0),
    policy_version          integer not null references policy_versions(version),
    classification_version  bigint not null references classification_versions(version),
    category                text not null
        check (category in ('productive', 'unproductive', 'neutral')),
    matched_rule_id         text,
    batch_id                uuid not null,
    ingested_at             timestamptz not null default now(),
    primary key (workstation_id, bucket_time),
    foreign key (classification_version, matched_rule_id)
        references classification_rules(classification_version, rule_id)
);
create index idx_snapshots_employee_time
    on activity_snapshots(employee_id, bucket_time);
create index idx_snapshots_bucket on activity_snapshots(bucket_time);

-- Batch audit trail. Rejected batches are recorded in a separate transaction
-- (their ingest transaction rolls back completely).
create table ingest_batches (
    batch_id        uuid primary key,
    status          text not null check (status in ('committed', 'rejected')),
    item_count      integer not null,
    accepted_count  integer not null default 0,
    duplicate_count integer not null default 0,
    filtered_count  integer not null default 0,
    http_status     integer,
    error_code      text,
    detail          text not null default '',
    received_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Summaries, derivable 1:1 from activity_snapshots.
-- Daily rows are keyed by the employee's LOCAL date; weekly rows use the
-- Monday of each employee's local calendar week, grouped per department.
-- ---------------------------------------------------------------------------

create table daily_employee_summary (
    employee_id                 bigint not null,
    local_date                  date not null,
    productive_count            bigint not null,
    unproductive_count          bigint not null,
    neutral_count               bigint not null,
    total_count                 bigint not null,
    policy_version_min          integer not null,
    policy_version_max          integer not null,
    classification_version_min  bigint not null,
    classification_version_max  bigint not null,
    -- Frozen buckets overlap purged raw history. They are retained as the
    -- authoritative complete statistic and must never be recomputed from raw
    -- rows (which would now be partial).
    frozen                      boolean not null default false,
    recomputed_at               timestamptz not null default now(),
    primary key (employee_id, local_date)
);

create table weekly_department_summary (
    department_id       bigint not null,
    week_start          date not null,
    productive_count    bigint not null,
    unproductive_count  bigint not null,
    neutral_count       bigint not null,
    total_count         bigint not null,
    frozen              boolean not null default false,
    recomputed_at       timestamptz not null default now(),
    primary key (department_id, week_start)
);

-- Raw-data retention marker. Once raw rows are purged this holds the oldest
-- surviving bucket_time; summary rows before/around it cannot be rebuilt and
-- must never be overwritten from partial history. Absent = no purge yet.
create table retention_state (
    id                smallint primary key default 1 check (id = 1),
    earliest_snapshot timestamptz not null,
    updated_at        timestamptz not null default now()
);

create table cleanup_events (
    id            bigint generated always as identity primary key,
    cutoff        timestamptz not null,
    deleted_rows  bigint not null,
    triggered_by  text not null default '',
    ran_at        timestamptz not null default now()
);
