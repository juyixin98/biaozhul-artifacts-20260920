-- GeoTerritory initial schema. MySQL 8 / InnoDB.
-- Coordinates are stored as DOUBLE (degrees); polygon rings are open JSON
-- arrays [{"lng":...,"lat":...}, ...] with the closure edge implicit.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version      VARCHAR(64) NOT NULL,
  applied_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS organizations (
  id              BIGINT       NOT NULL AUTO_INCREMENT,
  name            VARCHAR(128) NOT NULL,
  api_key         VARCHAR(64)  NOT NULL,
  active_set_seq  BIGINT       NOT NULL DEFAULT 0,
  created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_orgs_api_key (api_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS regions (
  id                 BIGINT       NOT NULL AUTO_INCREMENT,
  org_id             BIGINT       NOT NULL,
  name               VARCHAR(128) NOT NULL,
  active_version_id  BIGINT       NULL,
  created_at         DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_org_name (org_id, name),
  KEY idx_regions_org (org_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS region_versions (
  id          BIGINT      NOT NULL AUTO_INCREMENT,
  region_id   BIGINT      NOT NULL,
  org_id      BIGINT      NOT NULL,
  version     INT         NOT NULL,
  priority    INT         NOT NULL,
  polygon     JSON        NOT NULL,
  set_seq     BIGINT      NOT NULL DEFAULT 0,
  status      ENUM('building','active','superseded') NOT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_region_version (region_id, version),
  KEY idx_rv_org_status (org_id, status),
  KEY idx_rv_region (region_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS points (
  id                  BIGINT       NOT NULL AUTO_INCREMENT,
  org_id              BIGINT       NOT NULL,
  external_id         VARCHAR(128) NOT NULL,
  lng                 DOUBLE       NOT NULL,
  lat                 DOUBLE       NOT NULL,
  version             BIGINT       NOT NULL DEFAULT 1,
  region_id           BIGINT       NULL,
  region_version_id   BIGINT       NULL,
  assign_set_seq      BIGINT       NOT NULL DEFAULT 0,
  created_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_org_ext (org_id, external_id),
  KEY idx_points_org (org_id),
  KEY idx_points_region (region_id),
  -- Composite index for org-scoped coordinate scans (bbox / nearest).
  KEY idx_points_org_lat_lng (org_id, lat, lng)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reassign_jobs (
  id             BIGINT      NOT NULL AUTO_INCREMENT,
  org_id         BIGINT      NOT NULL,
  target_seq     BIGINT      NOT NULL,
  new_version_id BIGINT      NOT NULL,
  status         ENUM('pending','running','done','superseded','failed') NOT NULL,
  error          VARCHAR(1024) NOT NULL DEFAULT '',
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  started_at     DATETIME(3) NULL,
  finished_at    DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_jobs_status (status),
  KEY idx_jobs_org (org_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS assignment_staging (
  job_id             BIGINT NOT NULL,
  point_id           BIGINT NOT NULL,
  org_id             BIGINT NOT NULL,
  region_id          BIGINT NULL,
  region_version_id  BIGINT NULL,
  point_version      BIGINT NOT NULL,
  PRIMARY KEY (job_id, point_id),
  KEY idx_staging_org_job (org_id, job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
