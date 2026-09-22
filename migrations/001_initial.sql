-- Initial schema for employee activity anomaly detection.
-- All timestamps are stored in UTC. Local time / timezone boundaries are
-- computed explicitly from employees.time_zone (IANA zone, e.g. Asia/Shanghai).

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS departments (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name       VARCHAR(128) NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_departments_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS employees (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name       VARCHAR(128) NOT NULL,
    dept_id    BIGINT UNSIGNED NOT NULL,
    time_zone  VARCHAR(64) NOT NULL DEFAULT 'UTC',
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_employees_dept (dept_id),
    CONSTRAINT fk_employees_dept FOREIGN KEY (dept_id) REFERENCES departments (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS api_keys (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name       VARCHAR(128) NOT NULL,
    key_hash   CHAR(64) NOT NULL COMMENT 'sha256 hex of the raw key',
    role       ENUM('admin','analyst') NOT NULL,
    dept_id    BIGINT UNSIGNED NULL COMMENT 'NULL for admins (all departments)',
    active     TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_api_keys_hash (key_hash),
    KEY idx_api_keys_dept (dept_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Raw event ledger. The unique index on (source, event_id) provides
-- idempotency for duplicate reports; content_hash detects same-id/different-payload conflicts.
CREATE TABLE IF NOT EXISTS events (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source      VARCHAR(64) NOT NULL DEFAULT 'api',
    event_id    VARCHAR(128) NOT NULL,
    employee_id BIGINT UNSIGNED NOT NULL,
    event_type  ENUM('login','file_download','usb') NOT NULL,
    occurred_at DATETIME(3) NOT NULL COMMENT 'event time in UTC',
    metadata    JSON NULL,
    content_hash CHAR(64) NOT NULL,
    -- Random per-batch token: after an INSERT IGNORE, rows whose token matches
    -- the current batch were inserted by it; other tokens lost a race. Needed
    -- because multiple batches can share a millisecond-receive timestamp.
    ingest_token CHAR(32) NOT NULL DEFAULT '',
    processed   TINYINT(1) NOT NULL DEFAULT 0,
    received_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_events_source_id (source, event_id),
    KEY idx_events_emp_time (employee_id, occurred_at),
    KEY idx_events_type_time (event_type, occurred_at),
    KEY idx_events_processed (processed, received_at),
    CONSTRAINT fk_events_employee FOREIGN KEY (employee_id) REFERENCES employees (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Detection rules, versioned. Each edit bumps version; alerts keep the version
-- that fired them, and window_evaluations tracks which version evaluated a window.
CREATE TABLE IF NOT EXISTS rules (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    code        VARCHAR(32) NOT NULL COMMENT 'download_burst|night_activity|first_usb|statistical',
    name        VARCHAR(128) NOT NULL,
    description VARCHAR(512) NOT NULL DEFAULT '',
    enabled     TINYINT(1) NOT NULL DEFAULT 1,
    params      JSON NOT NULL,
    version     INT UNSIGNED NOT NULL DEFAULT 1,
    active      TINYINT(1) NOT NULL DEFAULT 1,
    created_at  DATETIME(3) NOT NULL,
    updated_at  DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rules_active_code (active, code),
    KEY idx_rules_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS alerts (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    rule_code     VARCHAR(32) NOT NULL,
    rule_version  INT UNSIGNED NOT NULL,
    employee_id   BIGINT UNSIGNED NOT NULL,
    status        ENUM('new','investigating','escalated','resolved','false_positive') NOT NULL DEFAULT 'new',
    severity      ENUM('low','medium','high') NOT NULL DEFAULT 'medium',
    title         VARCHAR(255) NOT NULL,
    -- Stable identity of (rule, employee, window); used for de-duplication.
    dedup_key     VARCHAR(190) NOT NULL,
    window_start  DATETIME(3) NULL COMMENT 'UTC start of the evaluated window, if windowed',
    window_end    DATETIME(3) NULL,
    evidence      JSON NOT NULL,
    fired_at      DATETIME(3) NOT NULL,
    acknowledged_at DATETIME(3) NULL COMMENT 'first move out of new',
    escalated_at  DATETIME(3) NULL,
    resolved_at   DATETIME(3) NULL,
    last_event_at DATETIME(3) NULL COMMENT 'newest event covered by the latest recomputation',
    updated_at    DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_alerts_dedup (dedup_key),
    KEY idx_alerts_emp (employee_id),
    KEY idx_alerts_status (status, fired_at),
    KEY idx_alerts_window (window_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Marks a (rule, employee, window) as evaluated under a rule version, so that
-- repeated sweeps / duplicated scheduler runs never reprocess the same window
-- for the same version. Recomputation bumps state on existing rows.
CREATE TABLE IF NOT EXISTS window_evaluations (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    rule_code    VARCHAR(32) NOT NULL,
    rule_version INT UNSIGNED NOT NULL,
    employee_id  BIGINT UNSIGNED NOT NULL,
    window_scope ENUM('burst','night','firstusb','stat') NOT NULL,
    window_date  DATE NOT NULL COMMENT 'local date for night/stat, UTC date for burst/firstusb',
    window_start DATETIME(3) NOT NULL,
    window_end   DATETIME(3) NOT NULL,
    evaluated_at DATETIME(3) NOT NULL,
    had_alert    TINYINT(1) NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_window_eval (rule_code, rule_version, employee_id, window_scope, window_date, window_start),
    KEY idx_window_eval_lookup (employee_id, window_scope, window_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS audit_logs (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    actor_key_id  BIGINT UNSIGNED NULL,
    actor_name    VARCHAR(128) NOT NULL DEFAULT '',
    action        VARCHAR(64) NOT NULL COMMENT 'alert.transition|rule.update|employee.timezone',
    entity_type   VARCHAR(32) NOT NULL DEFAULT '',
    entity_id     VARCHAR(64) NOT NULL DEFAULT '',
    detail        JSON NULL,
    created_at    DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_audit_action (action, created_at),
    KEY idx_audit_entity (entity_type, entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
