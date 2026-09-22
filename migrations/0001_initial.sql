/* GeoTerritory initial schema. */
/* MySQL 8 / InnoDB / utf8mb4. All time columns are UTC DATETIME(6). */

CREATE TABLE organizations (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name       VARCHAR(128)    NOT NULL,
  api_key    VARCHAR(128)    NOT NULL,
  created_at DATETIME(6)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_org_api_key (api_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE regions (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  org_id         BIGINT UNSIGNED NOT NULL,
  name           VARCHAR(128)    NOT NULL,
  latest_version INT             NOT NULL DEFAULT 0,
  priority       INT             NOT NULL,
  active         TINYINT(1)      NOT NULL DEFAULT 1,
  created_at     DATETIME(6)     NOT NULL,
  updated_at     DATETIME(6)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_region_org_name (org_id, name),
  KEY idx_region_org (org_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

/* Immutable snapshots of a region polygon. */
CREATE TABLE region_versions (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  region_id   BIGINT UNSIGNED NOT NULL,
  version     INT             NOT NULL,
  org_id      BIGINT UNSIGNED NOT NULL,
  name        VARCHAR(128)    NOT NULL,
  priority    INT             NOT NULL,
  vertices    JSON            NOT NULL,
  min_lat     DOUBLE          NOT NULL,
  min_lng     DOUBLE          NOT NULL,
  max_lat     DOUBLE          NOT NULL,
  max_lng     DOUBLE          NOT NULL,
  area_sq_deg DOUBLE          NOT NULL,
  created_at  DATETIME(6)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_region_version (region_id, version),
  KEY idx_rv_org_ver (org_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

/* Per-org catalog pointers. One row per org, seeded at version 0 (empty). */
CREATE TABLE region_catalog_state (
  org_id            BIGINT UNSIGNED NOT NULL,
  current_version   INT             NOT NULL DEFAULT 0,
  published_version INT             NOT NULL DEFAULT 0,
  updated_at        DATETIME(6)     NOT NULL,
  PRIMARY KEY (org_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

/* Immutable membership of a catalog version. */
CREATE TABLE catalog_entries (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  org_id            BIGINT UNSIGNED NOT NULL,
  catalog_version   INT             NOT NULL,
  region_version_id BIGINT UNSIGNED NOT NULL,
  region_id         BIGINT UNSIGNED NOT NULL,
  version           INT             NOT NULL,
  created_at        DATETIME(6)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_catalog_entry (org_id, catalog_version, region_version_id),
  KEY idx_ce_org_ver (org_id, catalog_version),
  KEY idx_ce_region (region_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

/* Durable reassignment tasks. The unique key on (org_id, status) leaves */
/* DONE/FAILED/CANCELLED out of the unique column set via a NULL sentinel: */
/* enforced instead by allowing only ONE active job; active statuses are */
/* PENDING/RUNNING. We emulate the partial unique index with a generated */
/* column that is NULL for terminal statuses. */
CREATE TABLE reassignment_jobs (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  org_id           BIGINT UNSIGNED NOT NULL,
  from_version     INT             NOT NULL,
  to_version       INT             NOT NULL,
  status           VARCHAR(16)     NOT NULL,
  total_points     BIGINT          NOT NULL DEFAULT 0,
  processed_points BIGINT          NOT NULL DEFAULT 0,
  error            TEXT            NULL,
  created_at       DATETIME(6)     NOT NULL,
  updated_at       DATETIME(6)     NOT NULL,
  heartbeat_at     DATETIME(6)     NULL,
  completed_at     DATETIME(6)     NULL,
  /* active_key is (org_id) for PENDING/RUNNING and NULL otherwise, so the */
  /* unique index guarantees at most one active job per org. */
  active_key       BIGINT UNSIGNED GENERATED ALWAYS AS
                     (CASE WHEN status IN ('PENDING','RUNNING') THEN org_id ELSE NULL END) STORED,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_job_org_active (active_key),
  KEY idx_job_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE points (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  org_id      BIGINT UNSIGNED NOT NULL,
  external_id VARCHAR(128)    NOT NULL,
  lat         DOUBLE          NOT NULL,
  lng         DOUBLE          NOT NULL,
  update_seq  INT             NOT NULL DEFAULT 1,
  created_at  DATETIME(6)     NOT NULL,
  updated_at  DATETIME(6)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_point_org_ext (org_id, external_id),
  KEY idx_point_org_geo (org_id, lat, lng)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

/* Assignment of a point under one catalog version. */
CREATE TABLE point_assignments (
  point_id         BIGINT UNSIGNED NOT NULL,
  org_id           BIGINT UNSIGNED NOT NULL,
  catalog_version  INT             NOT NULL,
  region_id        BIGINT UNSIGNED NOT NULL DEFAULT 0,
  region_version_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
  region_version   INT             NOT NULL DEFAULT 0,
  on_boundary      TINYINT(1)      NOT NULL DEFAULT 0,
  updated_at       DATETIME(6)     NOT NULL,
  PRIMARY KEY (org_id, catalog_version, point_id),
  KEY idx_pa_org_ver_region (org_id, catalog_version, region_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

