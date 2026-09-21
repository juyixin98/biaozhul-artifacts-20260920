-- ProofCycle initial schema.

CREATE TABLE IF NOT EXISTS users (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username      VARCHAR(64)     NOT NULL,
  password_hash VARCHAR(100)    NOT NULL,
  role          VARCHAR(16)     NOT NULL,
  token         VARCHAR(64)     NULL,
  created_at    DATETIME(3)     NOT NULL,
  updated_at    DATETIME(3)     NOT NULL,
  deleted_at    DATETIME(3)     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_users_username (username),
  UNIQUE KEY uk_users_token (token),
  KEY idx_users_deleted_at (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS jobs (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  title       VARCHAR(200)    NOT NULL,
  description VARCHAR(2000)   NOT NULL DEFAULT '',
  designer_id BIGINT UNSIGNED NOT NULL,
  pm_id       BIGINT UNSIGNED NOT NULL,
  status      VARCHAR(16)     NOT NULL DEFAULT 'open',
  created_at  DATETIME(3)     NOT NULL,
  updated_at  DATETIME(3)     NOT NULL,
  deleted_at  DATETIME(3)     NULL,
  PRIMARY KEY (id),
  KEY idx_jobs_designer (designer_id),
  KEY idx_jobs_pm (pm_id),
  KEY idx_jobs_deleted_at (deleted_at),
  CONSTRAINT fk_jobs_designer FOREIGN KEY (designer_id) REFERENCES users (id),
  CONSTRAINT fk_jobs_pm FOREIGN KEY (pm_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS job_reviewers (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id     BIGINT UNSIGNED NOT NULL,
  user_id    BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(3)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_reviewer (job_id, user_id),
  CONSTRAINT fk_jr_job FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE,
  CONSTRAINT fk_jr_user FOREIGN KEY (user_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS file_versions (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id       BIGINT UNSIGNED NOT NULL,
  version      INT             NOT NULL,
  filename     VARCHAR(255)    NOT NULL,
  kind         VARCHAR(8)      NOT NULL,
  size         BIGINT          NOT NULL,
  sha256       CHAR(64)        NOT NULL,
  storage_path VARCHAR(500)    NOT NULL,
  uploaded_by  BIGINT UNSIGNED NOT NULL,
  created_at   DATETIME(3)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_version (job_id, version),
  UNIQUE KEY uk_storage_path (storage_path),
  CONSTRAINT fk_fv_job FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS checklist_template_items (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code        VARCHAR(32)     NOT NULL,
  description VARCHAR(500)    NOT NULL,
  `order`     INT             NOT NULL DEFAULT 0,
  created_at  DATETIME(3)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ct_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rounds (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id            BIGINT UNSIGNED NOT NULL,
  version_id        BIGINT UNSIGNED NOT NULL,
  version_number    INT             NOT NULL,
  checklist_version VARCHAR(32)     NOT NULL,
  status            VARCHAR(16)     NOT NULL DEFAULT 'active',
  active_flag       TINYINT         NULL,
  created_at        DATETIME(3)     NOT NULL,
  approved_at       DATETIME(3)     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_active (job_id, active_flag),
  CONSTRAINT fk_rounds_job FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE,
  CONSTRAINT fk_rounds_version FOREIGN KEY (version_id) REFERENCES file_versions (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS round_items (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  round_id    BIGINT UNSIGNED NOT NULL,
  code        VARCHAR(32)     NOT NULL,
  description VARCHAR(500)    NOT NULL,
  sort_order  INT             NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_round_item (round_id, code),
  CONSTRAINT fk_ri_round FOREIGN KEY (round_id) REFERENCES rounds (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS opinions (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  round_id      BIGINT UNSIGNED NOT NULL,
  reviewer_id   BIGINT UNSIGNED NOT NULL,
  round_item_id BIGINT UNSIGNED NOT NULL,
  outcome       VARCHAR(8)      NOT NULL,
  reason        VARCHAR(1000)   NOT NULL DEFAULT '',
  version       INT             NOT NULL DEFAULT 1,
  created_at    DATETIME(3)     NOT NULL,
  updated_at    DATETIME(3)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_opinion (round_id, reviewer_id, round_item_id),
  KEY idx_opinions_reviewer (reviewer_id),
  CONSTRAINT fk_op_round FOREIGN KEY (round_id) REFERENCES rounds (id) ON DELETE CASCADE,
  CONSTRAINT fk_op_item FOREIGN KEY (round_item_id) REFERENCES round_items (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS approvals (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  round_id    BIGINT UNSIGNED NOT NULL,
  job_id      BIGINT UNSIGNED NOT NULL,
  approver_id BIGINT UNSIGNED NOT NULL,
  basis       TEXT            NOT NULL,
  created_at  DATETIME(3)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_approval_round (round_id),
  UNIQUE KEY uk_job_approval (job_id),
  CONSTRAINT fk_ap_round FOREIGN KEY (round_id) REFERENCES rounds (id) ON DELETE CASCADE,
  CONSTRAINT fk_ap_job FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    VARCHAR(32) NOT NULL,
  applied_at DATETIME(3) NOT NULL,
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
