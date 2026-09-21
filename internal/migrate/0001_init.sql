-- SiteVitals initial schema (MySQL 8.0+).
-- Metric values are NULL unless status = 'collected': missing metrics are
-- never zero-filled.

CREATE TABLE IF NOT EXISTS sites (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name        VARCHAR(128) NOT NULL,
  scheme_host VARCHAR(255) NOT NULL,
  description VARCHAR(512) NOT NULL DEFAULT '',
  enabled     TINYINT(1) NOT NULL DEFAULT 1,
  created_at  DATETIME(3) NULL,
  updated_at  DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_sites_scheme_host (scheme_host)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS allowed_urls (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  site_id     BIGINT UNSIGNED NOT NULL,
  url_pattern VARCHAR(1024) NOT NULL,
  note        VARCHAR(512) NOT NULL DEFAULT '',
  enabled     TINYINT(1) NOT NULL DEFAULT 1,
  created_at  DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_allowed_urls_site (site_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS jobs (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  target_url        VARCHAR(1024) NOT NULL,
  viewport          VARCHAR(16) NOT NULL,
  status            VARCHAR(16) NOT NULL DEFAULT 'queued',
  attempts          INT NOT NULL DEFAULT 0,
  max_attempts      INT NOT NULL DEFAULT 3,
  lease_holder      VARCHAR(128) NOT NULL DEFAULT '',
  fencing_token     BIGINT NOT NULL DEFAULT 0,
  leased_until      DATETIME(3) NULL,
  succeeded_run_id  BIGINT UNSIGNED NULL,
  last_error        VARCHAR(512) NOT NULL DEFAULT '',
  last_fail_class   VARCHAR(32) NOT NULL DEFAULT '',
  created_at        DATETIME(3) NULL,
  updated_at        DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_jobs_status (status),
  KEY idx_jobs_target (target_url(191)),
  KEY idx_jobs_lease_holder (lease_holder),
  KEY idx_jobs_succeeded_run (succeeded_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS runs (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id             BIGINT UNSIGNED NOT NULL,
  attempt            INT NOT NULL,
  viewport           VARCHAR(16) NOT NULL,
  target_url         VARCHAR(1024) NOT NULL,
  final_url          VARCHAR(1024) NOT NULL DEFAULT '',
  status             VARCHAR(16) NOT NULL DEFAULT 'running',
  lease_holder       VARCHAR(128) NOT NULL,
  fencing_token      BIGINT NOT NULL,
  redirect_hops      INT NOT NULL DEFAULT 0,
  fail_class         VARCHAR(32) NOT NULL DEFAULT '',
  fail_message       VARCHAR(512) NOT NULL DEFAULT '',
  resource_failures  INT NOT NULL DEFAULT 0,
  blocked_resources  INT NOT NULL DEFAULT 0,
  started_at         DATETIME(3) NULL,
  finished_at        DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY idx_job_attempt (job_id, attempt),
  KEY idx_runs_status (status),
  KEY idx_runs_target_status (target_url(191), viewport, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS metrics (
  id        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id    BIGINT UNSIGNED NOT NULL,
  name      VARCHAR(32) NOT NULL,
  status    VARCHAR(16) NOT NULL,
  value_ms  DOUBLE NULL,
  value_cls DOUBLE NULL,
  detail    TEXT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY idx_run_metric (run_id, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS resource_entries (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id         BIGINT UNSIGNED NOT NULL,
  url            VARCHAR(2048) NOT NULL,
  resource_type  VARCHAR(32) NOT NULL DEFAULT '',
  initiator      VARCHAR(64) NOT NULL DEFAULT '',
  status         VARCHAR(16) NOT NULL,
  http_status    INT NOT NULL DEFAULT 0,
  start_ms       DOUBLE NULL,
  end_ms         DOUBLE NULL,
  duration_ms    DOUBLE NULL,
  encoded_bytes  BIGINT NOT NULL DEFAULT 0,
  blocked_reason VARCHAR(255) NOT NULL DEFAULT '',
  PRIMARY KEY (id),
  KEY idx_resource_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS run_events (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id     BIGINT UNSIGNED NOT NULL,
  kind       VARCHAR(32) NOT NULL,
  message    VARCHAR(1024) NOT NULL,
  created_at DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_run_events_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS budgets (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  target_url    VARCHAR(1024) NOT NULL,
  viewport      VARCHAR(16) NOT NULL DEFAULT '',
  metric        VARCHAR(32) NOT NULL,
  threshold_ms  DOUBLE NULL,
  threshold_cls DOUBLE NULL,
  enabled       TINYINT(1) NOT NULL DEFAULT 1,
  created_at    DATETIME(3) NULL,
  updated_at    DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY idx_budget_key (target_url(191), viewport, metric)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS budget_evaluations (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  budget_id     BIGINT UNSIGNED NOT NULL,
  run_id        BIGINT UNSIGNED NOT NULL,
  job_id        BIGINT UNSIGNED NOT NULL,
  target_url    VARCHAR(1024) NOT NULL,
  viewport      VARCHAR(16) NOT NULL,
  metric        VARCHAR(32) NOT NULL,
  actual_ms     DOUBLE NULL,
  actual_cls    DOUBLE NULL,
  threshold_ms  DOUBLE NULL,
  threshold_cls DOUBLE NULL,
  exceeded      TINYINT(1) NOT NULL DEFAULT 0,
  skipped       TINYINT(1) NOT NULL DEFAULT 0,
  created_at    DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_beval_budget (budget_id),
  KEY idx_beval_run (run_id),
  KEY idx_beval_job (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     VARCHAR(64) NOT NULL,
  applied_at  DATETIME(3) NOT NULL,
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
