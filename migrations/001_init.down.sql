-- 回滚 ProofCycle 初始结构
SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS `approvals`;
DROP TABLE IF EXISTS `review_items`;
DROP TABLE IF EXISTS `reviews`;
DROP TABLE IF EXISTS `file_versions`;
DROP TABLE IF EXISTS `checklist_snapshot_items`;
DROP TABLE IF EXISTS `checklist_snapshots`;
DROP TABLE IF EXISTS `job_reviewers`;
DROP TABLE IF EXISTS `jobs`;
DROP TABLE IF EXISTS `checklist_items`;
DROP TABLE IF EXISTS `checklists`;
DROP TABLE IF EXISTS `users`;
SET FOREIGN_KEY_CHECKS = 1;
