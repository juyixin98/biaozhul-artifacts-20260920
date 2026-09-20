// Package service 实现 ProofCycle 的核心业务规则。
//
// 并发模型：所有改变作业审查状态的操作都在事务内先对作业行做带条件的
// 原子更新（lockJobTx / 签核的守卫更新），借此在数据库层串行化同一作业
// 上的并发写；检查清单与意见使用乐观锁（version 列），冲突明确返回 409。
package service

import (
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"gorm.io/gorm"

	"proofcycle/internal/models"
	"proofcycle/internal/storage"
)

var (
	ErrNotFound     = errors.New("resource not found")
	ErrForbidden    = errors.New("forbidden")
	ErrConflict     = errors.New("version conflict")
	ErrBadRequest   = errors.New("bad request")
	ErrInvalidState = errors.New("invalid state")
)

func badReq(format string, a ...any) error   { return fmt.Errorf("%w: %s", ErrBadRequest, fmt.Sprintf(format, a...)) }
func forbid(format string, a ...any) error   { return fmt.Errorf("%w: %s", ErrForbidden, fmt.Sprintf(format, a...)) }
func conflict(format string, a ...any) error { return fmt.Errorf("%w: %s", ErrConflict, fmt.Sprintf(format, a...)) }
func invalid(format string, a ...any) error  { return fmt.Errorf("%w: %s", ErrInvalidState, fmt.Sprintf(format, a...)) }
func notFound(format string, a ...any) error { return fmt.Errorf("%w: %s", ErrNotFound, fmt.Sprintf(format, a...)) }

type Service struct {
	db *gorm.DB
	st *storage.Storage
}

func New(db *gorm.DB, st *storage.Storage) *Service {
	return &Service{db: db, st: st}
}

// lockJobTx 对作业行做带状态条件的原子更新，作为作业级互斥锁。
// 作业不在审查中（不存在或已签核）时返回 ErrInvalidState。
func lockJobTx(tx *gorm.DB, jobID uint64) error {
	res := tx.Model(&models.Job{}).
		Where("id = ? AND status = ?", jobID, models.JobStatusInReview).
		Update("lock_version", gorm.Expr("lock_version + 1"))
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return invalid("job %d is not in review (missing or already approved)", jobID)
	}
	return nil
}

func (s *Service) loadJob(jobID uint64) (*models.Job, error) {
	var job models.Job
	if err := s.db.First(&job, jobID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, notFound("job %d", jobID)
		}
		return nil, err
	}
	return &job, nil
}

// requireParticipant 保证只有作业参与者（项目经理/设计师/指定审查员）可见。
func (s *Service) requireParticipant(job *models.Job, u *models.User) error {
	if u.ID == job.PMID || u.ID == job.DesignerID {
		return nil
	}
	var n int64
	if err := s.db.Model(&models.JobReviewer{}).
		Where("job_id = ? AND user_id = ?", job.ID, u.ID).Count(&n).Error; err != nil {
		return err
	}
	if n == 0 {
		return forbid("user %d is not a participant of job %d", u.ID, job.ID)
	}
	return nil
}

func requireReviewerTx(tx *gorm.DB, jobID, userID uint64) error {
	var n int64
	if err := tx.Model(&models.JobReviewer{}).
		Where("job_id = ? AND user_id = ?", jobID, userID).Count(&n).Error; err != nil {
		return err
	}
	if n == 0 {
		return forbid("user %d is not a designated reviewer of job %d", userID, jobID)
	}
	return nil
}

// lockCurrentRevisionTx 锁定作业并确认 revision 仍是当前版本；
// 已失效（被新修订取代）的版本拒绝一切写操作，但历史仍可读。
func lockCurrentRevisionTx(tx *gorm.DB, revisionID uint64) (*models.Revision, error) {
	var rev models.Revision
	if err := tx.First(&rev, revisionID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, notFound("revision %d", revisionID)
		}
		return nil, err
	}
	if err := lockJobTx(tx, rev.JobID); err != nil {
		return nil, err
	}
	var job models.Job
	if err := tx.First(&job, rev.JobID).Error; err != nil {
		return nil, err
	}
	if job.CurrentRevisionID != rev.ID {
		return nil, invalid("revision %d is superseded; review actions only apply to the current revision", rev.ID)
	}
	return &rev, nil
}

// ---------------------------------------------------------------------------
// 作业
// ---------------------------------------------------------------------------

type CreateJobInput struct {
	Title       string   `json:"title"`
	DesignerID  uint64   `json:"designer_id"`
	ReviewerIDs []uint64 `json:"reviewer_ids"`
	Checklist   []string `json:"checklist"`
}

func (s *Service) CreateJob(pm *models.User, in CreateJobInput) (*JobDetail, error) {
	if pm.Role != models.RolePM {
		return nil, forbid("only project managers can create jobs")
	}
	if strings.TrimSpace(in.Title) == "" {
		return nil, badReq("title is required")
	}
	if len(in.Checklist) == 0 {
		return nil, badReq("checklist must contain at least one item")
	}
	if len(in.ReviewerIDs) > 8 {
		return nil, badReq("a job can have at most 8 reviewers")
	}
	seen := map[uint64]bool{}
	for _, id := range in.ReviewerIDs {
		if id == 0 || seen[id] {
			return nil, badReq("invalid or duplicate reviewer id %d", id)
		}
		seen[id] = true
	}
	var designer models.User
	if err := s.db.First(&designer, in.DesignerID).Error; err != nil {
		return nil, badReq("designer %d not found", in.DesignerID)
	}
	for _, id := range in.ReviewerIDs {
		var u models.User
		if err := s.db.First(&u, id).Error; err != nil {
			return nil, badReq("reviewer %d not found", id)
		}
	}

	job := &models.Job{
		Title:      strings.TrimSpace(in.Title),
		DesignerID: in.DesignerID,
		PMID:       pm.ID,
		Status:     models.JobStatusInReview,
	}
	err := s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(job).Error; err != nil {
			return err
		}
		for _, id := range in.ReviewerIDs {
			if err := tx.Create(&models.JobReviewer{JobID: job.ID, UserID: id}).Error; err != nil {
				return err
			}
		}
		for i, content := range in.Checklist {
			content = strings.TrimSpace(content)
			if content == "" {
				return badReq("checklist item %d is empty", i+1)
			}
			item := models.ChecklistTemplateItem{JobID: job.ID, Seq: i + 1, Content: content}
			if err := tx.Create(&item).Error; err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return s.GetJob(pm, job.ID)
}

type JobDetail struct {
	*models.Job
	Revisions []models.Revision `json:"revisions"`
}

func (s *Service) GetJob(u *models.User, jobID uint64) (*JobDetail, error) {
	var job models.Job
	if err := s.db.Preload("Designer").Preload("PM").
		Preload("Reviewers.User").Preload("TemplateList").
		First(&job, jobID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, notFound("job %d", jobID)
		}
		return nil, err
	}
	if err := s.requireParticipant(&job, u); err != nil {
		return nil, err
	}
	var revs []models.Revision
	if err := s.db.Where("job_id = ?", job.ID).Order("version_no").Find(&revs).Error; err != nil {
		return nil, err
	}
	return &JobDetail{Job: &job, Revisions: revs}, nil
}

// ---------------------------------------------------------------------------
// 修订（文件版本）
// ---------------------------------------------------------------------------

// SubmitRevision 由设计师提交新修订。文件先落盘再写库；任一步失败都会
// 回滚数据库并清理已写入的文件，修订文件一旦创建不可覆盖。
func (s *Service) SubmitRevision(u *models.User, jobID uint64, displayName string, r io.Reader) (*models.Revision, error) {
	job, err := s.loadJob(jobID)
	if err != nil {
		return nil, err
	}
	if err := s.requireParticipant(job, u); err != nil {
		return nil, err
	}
	if u.ID != job.DesignerID {
		return nil, forbid("only the assigned designer can submit revisions")
	}

	var rev models.Revision
	var savedRel string
	err = s.db.Transaction(func(tx *gorm.DB) error {
		if err := lockJobTx(tx, jobID); err != nil {
			return err
		}
		var maxVer int
		if err := tx.Model(&models.Revision{}).Where("job_id = ?", jobID).
			Select("COALESCE(MAX(version_no), 0)").Scan(&maxVer).Error; err != nil {
			return err
		}
		rev = models.Revision{
			JobID:      jobID,
			VersionNo:  maxVer + 1,
			FileName:   storage.SanitizeName(displayName),
			Status:     models.RevisionActive,
			UploadedBy: u.ID,
		}
		if err := tx.Create(&rev).Error; err != nil {
			return err
		}
		// 文件落盘：失败则整个事务回滚，Save 自身会清理临时文件。
		rel, sum, size, mime, err := s.st.Save(jobID, rev.ID, r)
		if err != nil {
			return err
		}
		savedRel = rel
		updates := map[string]any{"file_path": rel, "sha256": sum, "size": size, "mime": mime}
		if err := tx.Model(&models.Revision{}).Where("id = ?", rev.ID).Updates(updates).Error; err != nil {
			return err
		}
		rev.FilePath, rev.SHA256, rev.Size, rev.Mime = rel, sum, size, mime

		// 检查清单快照：本轮审查绑定该文件版本。
		var tpl []models.ChecklistTemplateItem
		if err := tx.Where("job_id = ?", jobID).Order("seq").Find(&tpl).Error; err != nil {
			return err
		}
		for _, t := range tpl {
			item := models.ChecklistItem{
				RevisionID: rev.ID, Seq: t.Seq, Content: t.Content,
				Status: models.ChecklistPending, Version: 1,
			}
			if err := tx.Create(&item).Error; err != nil {
				return err
			}
		}
		// 每位指定审查员的新一轮状态。
		var reviewerIDs []uint64
		if err := tx.Model(&models.JobReviewer{}).Where("job_id = ?", jobID).
			Pluck("user_id", &reviewerIDs).Error; err != nil {
			return err
		}
		for _, rid := range reviewerIDs {
			st := models.ReviewerState{RevisionID: rev.ID, ReviewerID: rid, State: models.ReviewPending}
			if err := tx.Create(&st).Error; err != nil {
				return err
			}
		}
		// 旧版本失效（意见保留，但不再可写、不可作为签核依据）。
		if err := tx.Model(&models.Revision{}).
			Where("job_id = ? AND id <> ? AND status = ?", jobID, rev.ID, models.RevisionActive).
			Update("status", models.RevisionSuperseded).Error; err != nil {
			return err
		}
		return tx.Model(&models.Job{}).Where("id = ?", jobID).
			Update("current_revision_id", rev.ID).Error
	})
	if err != nil {
		if savedRel != "" {
			// 数据库失败后的补偿清理：删除已落盘但未被引用的文件。
			_ = s.st.Remove(savedRel)
		}
		return nil, err
	}
	return &rev, nil
}

func (s *Service) ListRevisions(u *models.User, jobID uint64) ([]models.Revision, error) {
	job, err := s.loadJob(jobID)
	if err != nil {
		return nil, err
	}
	if err := s.requireParticipant(job, u); err != nil {
		return nil, err
	}
	var revs []models.Revision
	if err := s.db.Where("job_id = ?", jobID).Order("version_no").Find(&revs).Error; err != nil {
		return nil, err
	}
	return revs, nil
}

func (s *Service) loadRevisionForJob(u *models.User, jobID, revisionID uint64) (*models.Job, *models.Revision, error) {
	job, err := s.loadJob(jobID)
	if err != nil {
		return nil, nil, err
	}
	if err := s.requireParticipant(job, u); err != nil {
		return nil, nil, err
	}
	var rev models.Revision
	if err := s.db.Where("id = ? AND job_id = ?", revisionID, jobID).First(&rev).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, nil, notFound("revision %d of job %d", revisionID, jobID)
		}
		return nil, nil, err
	}
	return job, &rev, nil
}

// OpenRevisionFile 返回版本文件的绝对路径（供下载），已做参与者鉴权与路径校验。
func (s *Service) OpenRevisionFile(u *models.User, jobID, revisionID uint64) (string, *models.Revision, error) {
	_, rev, err := s.loadRevisionForJob(u, jobID, revisionID)
	if err != nil {
		return "", nil, err
	}
	abs, err := s.st.AbsPath(rev.FilePath)
	if err != nil {
		return "", nil, err
	}
	return abs, rev, nil
}

// ---------------------------------------------------------------------------
// 检查清单
// ---------------------------------------------------------------------------

type ChecklistUpdateInput struct {
	Status          string `json:"status"`
	FailReason      string `json:"fail_reason"`
	ExpectedVersion int64  `json:"expected_version"`
}

// UpdateChecklistItem 更新检查项结论。仅允许 pass/fail/na；fail 必须填写原因；
// 携带 expected_version 做乐观锁，冲突返回 ErrConflict。
func (s *Service) UpdateChecklistItem(u *models.User, itemID uint64, in ChecklistUpdateInput) (*models.ChecklistItem, error) {
	st := models.ChecklistStatus(in.Status)
	switch st {
	case models.ChecklistPass, models.ChecklistFail, models.ChecklistNA:
	default:
		return nil, badReq("status must be one of pass, fail, na")
	}
	if st == models.ChecklistFail && strings.TrimSpace(in.FailReason) == "" {
		return nil, badReq("fail_reason is required when status is fail")
	}

	var item models.ChecklistItem
	err := s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.First(&item, itemID).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return notFound("checklist item %d", itemID)
			}
			return err
		}
		rev, err := lockCurrentRevisionTx(tx, item.RevisionID)
		if err != nil {
			return err
		}
		// 指定审查员或项目经理可更新检查项。
		var job models.Job
		if err := tx.First(&job, rev.JobID).Error; err != nil {
			return err
		}
		if u.ID != job.PMID {
			if err := requireReviewerTx(tx, rev.JobID, u.ID); err != nil {
				return err
			}
		}
		failReason := ""
		if st == models.ChecklistFail {
			failReason = strings.TrimSpace(in.FailReason)
		}
		res := tx.Model(&models.ChecklistItem{}).
			Where("id = ? AND version = ?", itemID, in.ExpectedVersion).
			Updates(map[string]any{
				"status":      st,
				"fail_reason": failReason,
				"updated_by":  u.ID,
				"version":     gorm.Expr("version + 1"),
			})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return conflict("checklist item %d was modified concurrently (expected version %d)", itemID, in.ExpectedVersion)
		}
		return tx.First(&item, itemID).Error
	})
	if err != nil {
		return nil, err
	}
	return &item, nil
}

// ---------------------------------------------------------------------------
// 审查意见
// ---------------------------------------------------------------------------

func (s *Service) AddComment(u *models.User, revisionID uint64, content string) (*models.Comment, error) {
	content = strings.TrimSpace(content)
	if content == "" {
		return nil, badReq("content is required")
	}
	var c models.Comment
	err := s.db.Transaction(func(tx *gorm.DB) error {
		rev, err := lockCurrentRevisionTx(tx, revisionID)
		if err != nil {
			return err
		}
		if err := requireReviewerTx(tx, rev.JobID, u.ID); err != nil {
			return err
		}
		c = models.Comment{RevisionID: revisionID, ReviewerID: u.ID, Content: content, Version: 1}
		return tx.Create(&c).Error
	})
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// UpdateComment 仅意见作者本人可修改，携带 expected_version 乐观锁。
func (s *Service) UpdateComment(u *models.User, commentID uint64, content string, expectedVersion int64) (*models.Comment, error) {
	content = strings.TrimSpace(content)
	if content == "" {
		return nil, badReq("content is required")
	}
	var c models.Comment
	err := s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.First(&c, commentID).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return notFound("comment %d", commentID)
			}
			return err
		}
		if _, err := lockCurrentRevisionTx(tx, c.RevisionID); err != nil {
			return err
		}
		if c.ReviewerID != u.ID {
			return forbid("only the author can update comment %d", commentID)
		}
		res := tx.Model(&models.Comment{}).
			Where("id = ? AND version = ?", commentID, expectedVersion).
			Updates(map[string]any{"content": content, "version": gorm.Expr("version + 1")})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return conflict("comment %d was modified concurrently (expected version %d)", commentID, expectedVersion)
		}
		return tx.First(&c, commentID).Error
	})
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// CompleteReview 审查员标记自己完成本轮审查。
func (s *Service) CompleteReview(u *models.User, revisionID uint64) error {
	return s.db.Transaction(func(tx *gorm.DB) error {
		rev, err := lockCurrentRevisionTx(tx, revisionID)
		if err != nil {
			return err
		}
		if err := requireReviewerTx(tx, rev.JobID, u.ID); err != nil {
			return err
		}
		now := time.Now()
		res := tx.Model(&models.ReviewerState{}).
			Where("revision_id = ? AND reviewer_id = ?", rev.ID, u.ID).
			Updates(map[string]any{"state": models.ReviewCompleted, "completed_at": &now})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return forbid("user %d is not a designated reviewer for revision %d", u.ID, rev.ID)
		}
		return nil
	})
}

// ---------------------------------------------------------------------------
// 签核
// ---------------------------------------------------------------------------

// Approve 最终签核。通过带条件的原子更新抢占签核权（并发下仅一个成功），
// 随后在持有作业行锁的事务内校验：所有指定审查员已完成当前版本审查、
// 当前版本没有未处理或失败的检查项。任何一项不满足都会回滚。
func (s *Service) Approve(u *models.User, jobID uint64) (*models.Approval, error) {
	job, err := s.loadJob(jobID)
	if err != nil {
		return nil, err
	}
	if u.ID != job.PMID {
		return nil, forbid("only the project manager of job %d can approve", jobID)
	}
	if u.ID == job.DesignerID {
		return nil, forbid("a designer cannot approve their own job")
	}

	var approval models.Approval
	err = s.db.Transaction(func(tx *gorm.DB) error {
		// 守卫更新：只有仍处于审查中的作业能被签核；并发签核只有一个能更新成功。
		res := tx.Model(&models.Job{}).
			Where("id = ? AND status = ?", jobID, models.JobStatusInReview).
			Update("status", models.JobStatusApproved)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return conflict("job %d is not awaiting approval (already approved or changed concurrently)", jobID)
		}
		var cur models.Job
		if err := tx.First(&cur, jobID).Error; err != nil {
			return err
		}
		if cur.CurrentRevisionID == 0 {
			return invalid("job %d has no revision to approve", jobID)
		}
		var total, done int64
		if err := tx.Model(&models.JobReviewer{}).Where("job_id = ?", jobID).Count(&total).Error; err != nil {
			return err
		}
		if err := tx.Model(&models.ReviewerState{}).
			Where("revision_id = ? AND state = ?", cur.CurrentRevisionID, models.ReviewCompleted).
			Count(&done).Error; err != nil {
			return err
		}
		if done < total {
			return invalid("cannot approve: only %d of %d designated reviewers completed the current revision", done, total)
		}
		var bad int64
		if err := tx.Model(&models.ChecklistItem{}).
			Where("revision_id = ? AND status IN ?", cur.CurrentRevisionID,
				[]models.ChecklistStatus{models.ChecklistPending, models.ChecklistFail}).
			Count(&bad).Error; err != nil {
			return err
		}
		if bad > 0 {
			return invalid("cannot approve: %d checklist item(s) of the current revision are pending or failed", bad)
		}
		var rev models.Revision
		if err := tx.First(&rev, cur.CurrentRevisionID).Error; err != nil {
			return err
		}
		approval = models.Approval{
			JobID: jobID, RevisionID: rev.ID, ApprovedBy: u.ID, SHA256: rev.SHA256,
		}
		return tx.Create(&approval).Error
	})
	if err != nil {
		return nil, err
	}
	if err := s.db.Preload("Approver").First(&approval, approval.ID).Error; err != nil {
		return nil, err
	}
	return &approval, nil
}

// ---------------------------------------------------------------------------
// 历史与报告
// ---------------------------------------------------------------------------

type RevisionHistory struct {
	Revision       models.Revision        `json:"revision"`
	Checklist      []models.ChecklistItem `json:"checklist"`
	Comments       []models.Comment       `json:"comments"`
	ReviewerStates []models.ReviewerState `json:"reviewer_states"`
}

// GetRevisionHistory 按版本查询审查历史；旧版本意见保留可查。
func (s *Service) GetRevisionHistory(u *models.User, jobID, revisionID uint64) (*RevisionHistory, error) {
	_, rev, err := s.loadRevisionForJob(u, jobID, revisionID)
	if err != nil {
		return nil, err
	}
	h := &RevisionHistory{Revision: *rev}
	if err := s.db.Where("revision_id = ?", rev.ID).Order("seq").Find(&h.Checklist).Error; err != nil {
		return nil, err
	}
	if err := s.db.Preload("Reviewer").Where("revision_id = ?", rev.ID).
		Order("id").Find(&h.Comments).Error; err != nil {
		return nil, err
	}
	if err := s.db.Preload("Reviewer").Where("revision_id = ?", rev.ID).
		Order("id").Find(&h.ReviewerStates).Error; err != nil {
		return nil, err
	}
	return h, nil
}

type Report struct {
	GeneratedAt    time.Time              `json:"generated_at"`
	Job            models.Job             `json:"job"`
	Revision       models.Revision        `json:"revision"`
	Checklist      []models.ChecklistItem `json:"checklist"`
	ReviewerStates []models.ReviewerState `json:"reviewer_states"`
	Comments       []models.Comment       `json:"comments"`
	Approval       *models.Approval       `json:"approval,omitempty"`
}

// GetReport 导出审查报告：文件摘要（SHA-256）、检查清单快照与签核依据。
// revisionID 为 0 时使用当前版本。仅作业参与者可导出。
func (s *Service) GetReport(u *models.User, jobID, revisionID uint64) (*Report, error) {
	job, err := s.loadJob(jobID)
	if err != nil {
		return nil, err
	}
	if err := s.requireParticipant(job, u); err != nil {
		return nil, err
	}
	revID := revisionID
	if revID == 0 {
		revID = job.CurrentRevisionID
	}
	var rev models.Revision
	if err := s.db.Where("id = ? AND job_id = ?", revID, jobID).First(&rev).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, notFound("revision %d of job %d", revID, jobID)
		}
		return nil, err
	}
	report := &Report{
		GeneratedAt: time.Now(),
		Revision:    rev,
	}
	if err := s.db.Preload("Designer").Preload("PM").
		Preload("Reviewers.User").First(&report.Job, jobID).Error; err != nil {
		return nil, err
	}
	if err := s.db.Where("revision_id = ?", rev.ID).Order("seq").Find(&report.Checklist).Error; err != nil {
		return nil, err
	}
	if err := s.db.Preload("Reviewer").Where("revision_id = ?", rev.ID).
		Order("id").Find(&report.ReviewerStates).Error; err != nil {
		return nil, err
	}
	if err := s.db.Preload("Reviewer").Where("revision_id = ?", rev.ID).
		Order("id").Find(&report.Comments).Error; err != nil {
		return nil, err
	}
	var approval models.Approval
	err = s.db.Preload("Approver").Where("job_id = ? AND revision_id = ?", jobID, rev.ID).First(&approval).Error
	switch {
	case err == nil:
		report.Approval = &approval
	case errors.Is(err, gorm.ErrRecordNotFound):
	default:
		return nil, err
	}
	return report, nil
}
