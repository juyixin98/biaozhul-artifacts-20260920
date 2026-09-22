-- CareOps assessment schema. DDL is append-only; add future changes as
-- 002_*.sql etc. Papers are frozen via paper_questions snapshots (no FK to
-- questions is required for grading), so bank edits never reach history.

CREATE TABLE IF NOT EXISTS organizations (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  name VARCHAR(128) NOT NULL,
  timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS users (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id INT UNSIGNED NULL,
  name VARCHAR(128) NOT NULL,
  role ENUM('student','supervisor','admin') NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_users_org (organization_id),
  CONSTRAINT fk_users_org FOREIGN KEY (organization_id) REFERENCES organizations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS questions (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id INT UNSIGNED NULL,
  type ENUM('single','multiple','boolean') NOT NULL,
  difficulty TINYINT UNSIGNED NOT NULL,
  stem TEXT NOT NULL,
  options JSON NOT NULL,
  answer JSON NOT NULL,
  explanation TEXT NOT NULL,
  active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_questions_org (organization_id),
  KEY idx_questions_active (active),
  KEY idx_questions_difficulty (difficulty),
  CONSTRAINT fk_questions_org FOREIGN KEY (organization_id) REFERENCES organizations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS papers (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id INT UNSIGNED NULL,
  title VARCHAR(191) NOT NULL,
  selection_seed VARCHAR(64) NOT NULL,
  question_count TINYINT UNSIGNED NOT NULL DEFAULT 20,
  min_hard_ratio DECIMAL(4,3) NOT NULL DEFAULT 0.300,
  points_per_question TINYINT UNSIGNED NOT NULL DEFAULT 5,
  duration_minutes TINYINT UNSIGNED NOT NULL DEFAULT 30,
  status ENUM('active','retired') NOT NULL DEFAULT 'active',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_papers_org (organization_id),
  CONSTRAINT fk_papers_org FOREIGN KEY (organization_id) REFERENCES organizations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS paper_questions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  paper_id INT UNSIGNED NOT NULL,
  position TINYINT UNSIGNED NOT NULL,
  source_question_id INT UNSIGNED NULL,
  type ENUM('single','multiple','boolean') NOT NULL,
  difficulty TINYINT UNSIGNED NOT NULL,
  stem TEXT NOT NULL,
  options JSON NOT NULL,
  answer JSON NOT NULL,
  explanation TEXT NOT NULL,
  points TINYINT UNSIGNED NOT NULL,
  scoring_rule ENUM('exact') NOT NULL DEFAULT 'exact',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_paper_position (paper_id, position),
  KEY idx_pq_source (source_question_id),
  CONSTRAINT fk_pq_paper FOREIGN KEY (paper_id) REFERENCES papers(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS exams (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  paper_id INT UNSIGNED NOT NULL,
  user_id INT UNSIGNED NOT NULL,
  started_at DATETIME(3) NOT NULL,
  deadline_at DATETIME(3) NOT NULL,
  submitted_at DATETIME(3) NULL,
  presentation_seed VARCHAR(64) NOT NULL,
  status ENUM('in_progress','submitted','expired') NOT NULL DEFAULT 'in_progress',
  final_score_version_id BIGINT UNSIGNED NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_exams_user_status (user_id, status),
  KEY idx_exams_paper (paper_id),
  KEY idx_exams_deadline (status, deadline_at),
  CONSTRAINT fk_exams_paper FOREIGN KEY (paper_id) REFERENCES papers(id),
  CONSTRAINT fk_exams_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS daily_attempt_counts (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id INT UNSIGNED NOT NULL,
  day_key CHAR(10) NOT NULL,
  count INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uq_day_count (user_id, day_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS score_versions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  exam_id INT UNSIGNED NOT NULL,
  version INT UNSIGNED NOT NULL,
  score TINYINT UNSIGNED NOT NULL,
  correct_count TINYINT UNSIGNED NOT NULL,
  wrong_count TINYINT UNSIGNED NOT NULL,
  grading_detail JSON NOT NULL,
  source ENUM('auto','review') NOT NULL,
  reason VARCHAR(500) NULL,
  reviewer_id INT UNSIGNED NULL,
  created_at DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_exam_version (exam_id, version),
  KEY idx_sv_exam (exam_id),
  CONSTRAINT fk_sv_exam FOREIGN KEY (exam_id) REFERENCES exams(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Circular FK (exams.final_score_version_id -> score_versions) is added
-- after both tables exist.
ALTER TABLE exams
  ADD CONSTRAINT fk_exams_final_version
  FOREIGN KEY (final_score_version_id) REFERENCES score_versions(id);

CREATE TABLE IF NOT EXISTS submissions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  exam_id INT UNSIGNED NOT NULL,
  request_id VARCHAR(128) NOT NULL,
  content_hash CHAR(64) NOT NULL,
  answers JSON NOT NULL,
  score_version_id BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_submission_request (exam_id, request_id),
  KEY idx_sub_version (score_version_id),
  CONSTRAINT fk_sub_exam FOREIGN KEY (exam_id) REFERENCES exams(id),
  CONSTRAINT fk_sub_version FOREIGN KEY (score_version_id) REFERENCES score_versions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS wrong_questions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  exam_id INT UNSIGNED NOT NULL,
  user_id INT UNSIGNED NOT NULL,
  paper_question_id BIGINT UNSIGNED NOT NULL,
  given_answer JSON NOT NULL,
  correct_answer JSON NOT NULL,
  score_version INT UNSIGNED NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_wq_user (user_id),
  UNIQUE KEY uq_wq_exam_pq_version (exam_id, paper_question_id, score_version),
  CONSTRAINT fk_wq_exam FOREIGN KEY (exam_id) REFERENCES exams(id),
  CONSTRAINT fk_wq_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT fk_wq_pq FOREIGN KEY (paper_question_id) REFERENCES paper_questions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS certificates (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cert_no VARCHAR(40) NOT NULL,
  exam_id INT UNSIGNED NOT NULL,
  user_id INT UNSIGNED NOT NULL,
  paper_id INT UNSIGNED NOT NULL,
  score TINYINT UNSIGNED NOT NULL,
  mastery_at_issue TINYINT UNSIGNED NOT NULL,
  issued_at DATETIME(3) NOT NULL,
  valid_from DATETIME(3) NOT NULL,
  valid_until DATETIME(3) NOT NULL,
  status ENUM('valid','expired','revoked') NOT NULL DEFAULT 'valid',
  revoke_reason VARCHAR(500) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_cert_exam (exam_id),
  UNIQUE KEY uq_cert_no (cert_no),
  KEY idx_cert_user_status (user_id, status),
  CONSTRAINT fk_cert_exam FOREIGN KEY (exam_id) REFERENCES exams(id),
  CONSTRAINT fk_cert_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT fk_cert_paper FOREIGN KEY (paper_id) REFERENCES papers(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
