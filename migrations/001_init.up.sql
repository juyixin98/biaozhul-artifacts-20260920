-- ProofCycle 初始结构迁移（MySQL 8.x / InnoDB / utf8mb4）
-- 与 internal/domain 中的 GORM 实体一致；应用默认启动时 AutoMigrate，
-- 本文件用于关闭 AutoMigrate 的环境（PROOFCYCLE_AUTO_MIGRATE=false）手工初始化。
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- 用户：设计师 / 项目经理 / 审查员
CREATE TABLE IF NOT EXISTS `users` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `role` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_users_role` (`role`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 检查清单模板
CREATE TABLE IF NOT EXISTS `checklists` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `version` bigint NOT NULL DEFAULT '1',
  `created_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `checklist_items` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `checklist_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `order_no` bigint NOT NULL,
  `code` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `text` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_checklist_items_checklist_id` (`checklist_id`),
  CONSTRAINT `fk_checklists_items` FOREIGN KEY (`checklist_id`) REFERENCES `checklists` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 作业
CREATE TABLE IF NOT EXISTS `jobs` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `description` text COLLATE utf8mb4_unicode_ci,
  `designer_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `pm_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `checklist_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'in_review',
  `current_version_no` bigint NOT NULL DEFAULT '0',
  `created_at` datetime(3) DEFAULT NULL,
  `updated_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_jobs_checklist_id` (`checklist_id`),
  KEY `idx_jobs_designer_id` (`designer_id`),
  KEY `idx_jobs_pm_id` (`pm_id`),
  CONSTRAINT `fk_jobs_designer` FOREIGN KEY (`designer_id`) REFERENCES `users` (`id`),
  CONSTRAINT `fk_jobs_pm` FOREIGN KEY (`pm_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 作业指定审查员（每作业最多 8 名，由应用层校验）
CREATE TABLE IF NOT EXISTS `job_reviewers` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `job_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_job_reviewer` (`job_id`,`user_id`),
  KEY `idx_job_reviewers_job_id` (`job_id`),
  KEY `idx_job_reviewers_user_id` (`user_id`),
  CONSTRAINT `fk_job_reviewers_job` FOREIGN KEY (`job_id`) REFERENCES `jobs` (`id`),
  CONSTRAINT `fk_job_reviewers_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 每轮审查绑定的清单快照（不可变）
CREATE TABLE IF NOT EXISTS `checklist_snapshots` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `job_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `version_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `checklist_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `checklist_name` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_version` bigint NOT NULL,
  `created_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_snapshot_version` (`version_id`),
  KEY `idx_checklist_snapshots_job_id` (`job_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `checklist_snapshot_items` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `snapshot_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `order_no` bigint NOT NULL,
  `code` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `text` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_snapshot_order` (`order_no`),
  KEY `idx_checklist_snapshot_items_snapshot_id` (`snapshot_id`),
  CONSTRAINT `fk_checklist_snapshots_items` FOREIGN KEY (`snapshot_id`) REFERENCES `checklist_snapshots` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 文件版本：修订逐版本新增，永不覆盖
CREATE TABLE IF NOT EXISTS `file_versions` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `job_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `version_no` bigint NOT NULL,
  `uploaded_by_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `file_name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `stored_path` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `content_type` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `size_bytes` bigint NOT NULL,
  `sha256` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `snapshot_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `superseded_by_id` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_job_verno` (`job_id`,`version_no`),
  KEY `idx_file_versions_job_id` (`job_id`),
  KEY `idx_file_versions_uploaded_by_id` (`uploaded_by_id`),
  KEY `idx_file_versions_sha256` (`sha256`),
  KEY `idx_file_versions_superseded_by_id` (`superseded_by_id`),
  KEY `fk_file_versions_snapshot` (`snapshot_id`),
  CONSTRAINT `fk_file_versions_snapshot` FOREIGN KEY (`snapshot_id`) REFERENCES `checklist_snapshots` (`id`),
  CONSTRAINT `fk_file_versions_uploaded_by` FOREIGN KEY (`uploaded_by_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 审查意见（每版本每审查员至多一份；row_version 乐观锁）
CREATE TABLE IF NOT EXISTS `reviews` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `version_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `reviewer_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `row_version` bigint NOT NULL DEFAULT '1',
  `submitted_at` datetime(3) DEFAULT NULL,
  `updated_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_version_reviewer` (`version_id`,`reviewer_id`),
  KEY `idx_reviews_reviewer_id` (`reviewer_id`),
  KEY `idx_reviews_version_id` (`version_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `review_items` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `review_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `snapshot_item_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `result` varchar(8) COLLATE utf8mb4_unicode_ci NOT NULL, -- pass | fail | na
  `fail_reason` text COLLATE utf8mb4_unicode_ci,           -- result=fail 时必填（应用层校验）
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_review_item` (`review_id`,`snapshot_item_id`),
  KEY `idx_review_items_review_id` (`review_id`),
  CONSTRAINT `fk_reviews_items` FOREIGN KEY (`review_id`) REFERENCES `reviews` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 最终签核（每版本至多一条，唯一约束兜底防并发双签）
CREATE TABLE IF NOT EXISTS `approvals` (
  `id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `job_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `version_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `approver_id` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `basis` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_approval_version` (`job_id`,`version_id`),
  KEY `idx_approvals_job_id` (`job_id`),
  KEY `idx_approvals_approver_id` (`approver_id`),
  CONSTRAINT `fk_approvals_approver` FOREIGN KEY (`approver_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;
