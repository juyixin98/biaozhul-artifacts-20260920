-- ProofCycle initial schema.
-- All timestamps are UTC DATETIME(6). No FK constraints: referential
-- integrity is enforced by the service inside serializing transactions,
-- which also makes crash cleanup order-independent.

CREATE TABLE IF NOT EXISTS users (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  name       VARCHAR(100) NOT NULL,
  role       VARCHAR(20)  NOT NULL,
  api_token  VARCHAR(128) NOT NULL,
  created_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_api_token (api_token),
  CONSTRAINT chk_users_role CHECK (role IN ('designer', 'pm', 'reviewer'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS jobs (
  id              BIGINT       NOT NULL AUTO_INCREMENT,
  name            VARCHAR(200) NOT NULL,
  designer_id     BIGINT       NOT NULL,
  pm_id           BIGINT       NOT NULL,
  status          VARCHAR(20)  NOT NULL DEFAULT 'in_review',
  current_version INT          NOT NULL DEFAULT 0,
  created_at      DATETIME(6)  NOT NULL,
  updated_at      DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_job_designer_name (designer_id, name),
  KEY idx_jobs_pm (pm_id),
  CONSTRAINT chk_jobs_status CHECK (status IN ('in_review', 'approved'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS job_reviewers (
  job_id      BIGINT      NOT NULL,
  reviewer_id BIGINT      NOT NULL,
  created_at  DATETIME(6) NOT NULL,
  PRIMARY KEY (job_id, reviewer_id),
  KEY idx_job_reviewers_reviewer (reviewer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS job_checklist_items (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  job_id      BIGINT       NOT NULL,
  item_order  INT          NOT NULL,
  code        VARCHAR(40)  NOT NULL,
  description VARCHAR(500) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_job_checklist_code (job_id, code),
  KEY idx_job_checklist_order (job_id, item_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS file_versions (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  job_id      BIGINT       NOT NULL,
  version     INT          NOT NULL,
  file_name   VARCHAR(255) NOT NULL,
  stored_path VARCHAR(512) NOT NULL,
  sha256      CHAR(64)     NOT NULL,
  size_bytes  BIGINT       NOT NULL,
  mime_type   VARCHAR(80)  NOT NULL,
  uploaded_by BIGINT       NOT NULL,
  status      VARCHAR(20)  NOT NULL DEFAULT 'ready',
  created_at  DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_job_version (job_id, version),
  KEY idx_version_job_status (job_id, status),
  CONSTRAINT chk_versions_status CHECK (status IN ('superseded', 'ready', 'approved')),
  CONSTRAINT chk_versions_mime CHECK (mime_type IN ('application/pdf', 'image/png'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS review_rounds (
  id            BIGINT      NOT NULL AUTO_INCREMENT,
  job_id        BIGINT      NOT NULL,
  version       INT         NOT NULL,
  file_version_id BIGINT    NOT NULL,
  status        VARCHAR(20) NOT NULL DEFAULT 'open',
  signoff_by    BIGINT      NULL,
  signoff_at    DATETIME(6) NULL,
  signoff_basis TEXT        NULL,
  created_at    DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_round_job_version (job_id, version),
  KEY idx_rounds_file_version (file_version_id),
  CONSTRAINT chk_rounds_status CHECK (status IN ('open', 'superseded', 'approved'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS checklist_items (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  round_id    BIGINT       NOT NULL,
  item_order  INT          NOT NULL,
  code        VARCHAR(40)  NOT NULL,
  description VARCHAR(500) NOT NULL,
  PRIMARY KEY (id),
  KEY idx_checklist_round_order (round_id, item_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS opinions (
  id                BIGINT        NOT NULL AUTO_INCREMENT,
  round_id          BIGINT        NOT NULL,
  reviewer_id       BIGINT        NOT NULL,
  checklist_item_id BIGINT        NOT NULL,
  verdict           VARCHAR(20)   NOT NULL,
  reason            VARCHAR(1000) NULL,
  row_version       INT           NOT NULL DEFAULT 1,
  created_at        DATETIME(6)   NOT NULL,
  updated_at        DATETIME(6)   NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_opinion (round_id, reviewer_id, checklist_item_id),
  KEY idx_opinions_reviewer (reviewer_id),
  CONSTRAINT chk_opinions_verdict CHECK (verdict IN ('pass', 'fail', 'na'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS signoffs (
  id         BIGINT      NOT NULL AUTO_INCREMENT,
  round_id   BIGINT      NOT NULL,
  job_id     BIGINT      NOT NULL,
  version    INT         NOT NULL,
  pm_user_id BIGINT      NOT NULL,
  basis      TEXT        NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_signoffs_round (round_id),
  KEY idx_signoffs_job (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    VARCHAR(255) NOT NULL,
  applied_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
