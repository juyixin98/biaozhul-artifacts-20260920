-- SiteVitals initial schema (MySQL 8.x, InnoDB, utf8mb4).
--
-- The application applies this schema automatically on startup via GORM
-- AutoMigrate (AUTO_MIGRATE=true). This file documents the canonical schema,
-- is mounted into /docker-entrypoint-initdb.d for fresh MySQL containers, and
-- can be applied manually:
--
--   mysql -u sitevitals -p sitevitals < deploy/sql/001_init.sql
--
-- Migration policy: additive changes go through AutoMigrate; destructive
-- changes get a new numbered file here.

-- The application's MySQL user must be able to create the schema if this is
-- applied to a bare server. In Docker the database is pre-created by
-- MYSQL_DATABASE, so these two lines are harmless no-ops there.
CREATE DATABASE IF NOT EXISTS sitevitals CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE sitevitals;

CREATE TABLE IF NOT EXISTS sites (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name        VARCHAR(128) NOT NULL,
  origin      VARCHAR(255) NOT NULL COMMENT 'scheme://host[:port]',
  path_prefix VARCHAR(512) NOT NULL DEFAULT '/',
  description VARCHAR(512) NULL,
  enabled     TINYINT(1) NOT NULL DEFAULT 1,
  created_at  DATETIME(3) NULL,
  updated_at  DATETIME(3) NULL,
  deleted_at  DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_sites_origin_prefix (origin, path_prefix),
  KEY idx_sites_deleted_at (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tasks (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  url          VARCHAR(2048) NOT NULL COMMENT 'normalized target URL',
  final_url    VARCHAR(2048) NULL,
  viewport     VARCHAR(16) NOT NULL COMMENT 'mobile|tablet|desktop',
  status       VARCHAR(16) NOT NULL COMMENT 'queued|running|succeeded|failed|dead',
  priority     INT NOT NULL DEFAULT 0,
  attempts     INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 3,
  owner        VARCHAR(64) NULL,
  lease_token  VARCHAR(64) NULL COMMENT 'rotated on every claim; guards late commits',
  leased_until DATETIME(3) NULL COMMENT 'expired leases are reclaimed',
  run_after    DATETIME(3) NOT NULL COMMENT 'earliest dispatch time (backoff)',
  started_at   DATETIME(3) NULL,
  finished_at  DATETIME(3) NULL,
  error_code   VARCHAR(64) NULL,
  error_msg    VARCHAR(1024) NULL,
  created_at   DATETIME(3) NULL,
  updated_at   DATETIME(3) NULL,
  deleted_at   DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_tasks_status (status),
  KEY idx_tasks_run_after (run_after),
  KEY idx_tasks_dispatch (url(191), viewport, status, created_at),
  KEY idx_leased_until (leased_until),
  KEY idx_tasks_deleted_at (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS runs (
  id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_id              BIGINT UNSIGNED NOT NULL,
  attempt_no           INT NOT NULL,
  status               VARCHAR(16) NOT NULL COMMENT 'running|succeeded|failed|abandoned',
  owner                VARCHAR(64) NOT NULL,
  viewport             VARCHAR(16) NOT NULL,
  url                  VARCHAR(2048) NOT NULL COMMENT 'normalized URL (comparison key)',
  final_url            VARCHAR(2048) NULL,
  started_at           DATETIME(3) NOT NULL,
  finished_at          DATETIME(3) NULL,
  nav_duration_ms      DECIMAL(12,3) NULL COMMENT 'NULL = not measured, never zero-filled',
  fcp_ms               DECIMAL(12,3) NULL,
  lcp_ms               DECIMAL(12,3) NULL,
  cls                  DECIMAL(12,4) NULL,
  long_task_count      INT NULL,
  long_task_total_ms   DECIMAL(12,3) NULL,
  long_task_max_ms     DECIMAL(12,3) NULL,
  window_start_offset_ms DECIMAL(12,3) NOT NULL DEFAULT 0,
  window_end           DATETIME(3) NULL COMMENT 'end of long-task observation window',
  metric_status_json   LONGTEXT NULL,
  warnings_json        LONGTEXT NULL,
  redirects_json       LONGTEXT NULL,
  violations_json      LONGTEXT NULL,
  resources_json       LONGTEXT NULL,
  error_code           VARCHAR(64) NULL,
  error_msg            VARCHAR(1024) NULL,
  created_at           DATETIME(3) NULL,
  updated_at           DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_runs_task_attempt (task_id, attempt_no),
  KEY idx_runs_compare (url(191), viewport, status),
  KEY idx_runs_status (status),
  KEY idx_runs_error_code (error_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reports (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_id    BIGINT UNSIGNED NOT NULL,
  run_id     BIGINT UNSIGNED NOT NULL,
  markdown   LONGTEXT NOT NULL,
  created_at DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_reports_task (task_id) COMMENT 'exactly one report per task',
  UNIQUE KEY uniq_reports_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS budgets (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  site_id           BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '0 = global defaults',
  fcp_ms            DECIMAL(12,3) NULL,
  lcp_ms            DECIMAL(12,3) NULL,
  cls               DECIMAL(12,4) NULL,
  nav_duration_ms   DECIMAL(12,3) NULL,
  long_task_total_ms DECIMAL(12,3) NULL,
  created_at        DATETIME(3) NULL,
  updated_at        DATETIME(3) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_budgets_site (site_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS budget_alerts (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id     BIGINT UNSIGNED NOT NULL,
  task_id    BIGINT UNSIGNED NOT NULL,
  metric     VARCHAR(32) NOT NULL,
  actual     DECIMAL(12,4) NOT NULL,
  threshold  DECIMAL(12,4) NOT NULL,
  severity   VARCHAR(16) NOT NULL DEFAULT 'warn',
  created_at DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_budget_alerts_run (run_id),
  KEY idx_budget_alerts_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS comparisons (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  url          VARCHAR(2048) NOT NULL,
  viewport     VARCHAR(16) NOT NULL,
  baseline_run BIGINT UNSIGNED NOT NULL,
  current_run  BIGINT UNSIGNED NOT NULL,
  diff_json    JSON NOT NULL,
  created_at   DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_comparisons_url_viewport (url(191), viewport)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
