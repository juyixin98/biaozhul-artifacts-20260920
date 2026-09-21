package service

import (
	"errors"

	"gorm.io/gorm"

	"proofcycle/internal/domain"
	"proofcycle/internal/util"
)

// MaxReviewers 每个作业最多 8 名审查员。
const MaxReviewers = 8

// JobService 作业创建与查询。
type JobService struct {
	db *gorm.DB
}

// CreateJobInput 创建作业参数。
type CreateJobInput struct {
	Name        string
	Description string
	DesignerID  string
	PMID        string
	ReviewerIDs []string
	ChecklistID string
}

// Create 校验成员角色与人数后创建作业（in_review，尚无文件版本）。
func (s *JobService) Create(in CreateJobInput) (*domain.Job, []domain.User, error) {
	if in.Name == "" {
		return nil, nil, errors.Join(ErrValidation, errors.New("job name is required"))
	}
	if len(in.ReviewerIDs) == 0 {
		return nil, nil, ErrReviewerMissing
	}
	if len(in.ReviewerIDs) > MaxReviewers {
		return nil, nil, ErrTooManyReviewers
	}

	var designer, pm domain.User
	if err := s.db.First(&designer, "id = ?", in.DesignerID).Error; err != nil {
		return nil, nil, ErrUnknownUser
	}
	if designer.Role != domain.RoleDesigner {
		return nil, nil, errors.Join(ErrValidation, errors.New("designer user must have designer role"))
	}
	if err := s.db.First(&pm, "id = ?", in.PMID).Error; err != nil {
		return nil, nil, ErrUnknownUser
	}
	if pm.Role != domain.RolePM {
		return nil, nil, errors.Join(ErrValidation, errors.New("pm user must have pm role"))
	}

	seen := map[string]bool{in.DesignerID: true, in.PMID: true}
	reviewers := make([]domain.User, 0, len(in.ReviewerIDs))
	for _, rid := range in.ReviewerIDs {
		if seen[rid] {
			if rid == in.DesignerID || rid == in.PMID {
				return nil, nil, ErrDistinctMembers
			}
			return nil, nil, ErrDuplicateReviewer
		}
		seen[rid] = true
		var u domain.User
		if err := s.db.First(&u, "id = ?", rid).Error; err != nil {
			return nil, nil, ErrUnknownUser
		}
		if u.Role != domain.RoleReviewer {
			return nil, nil, errors.Join(ErrValidation, errors.New("reviewer users must have reviewer role"))
		}
		reviewers = append(reviewers, u)
	}

	var cl domain.Checklist
	if err := s.db.First(&cl, "id = ?", in.ChecklistID).Error; err != nil {
		return nil, nil, ErrUnknownChecklist
	}

	job := &domain.Job{
		ID:               util.NewID(),
		Name:             in.Name,
		Description:      in.Description,
		DesignerID:       in.DesignerID,
		PMID:             in.PMID,
		ChecklistID:      in.ChecklistID,
		Status:           domain.StatusInReview,
		CurrentVersionNo: 0,
	}
	err := s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(job).Error; err != nil {
			return err
		}
		for _, r := range reviewers {
			jr := domain.JobReviewer{ID: util.NewID(), JobID: job.ID, UserID: r.ID}
			if err := tx.Create(&jr).Error; err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	return job, reviewers, nil
}

// Get 取作业（含成员），不存在返回 ErrNotFound。
func (s *JobService) Get(jobID string) (*domain.Job, error) {
	var job domain.Job
	err := s.db.
		Preload("Designer").Preload("PM").
		First(&job, "id = ?", jobID).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &job, nil
}

// Reviewers 返回作业指定审查员。
func (s *JobService) Reviewers(jobID string) ([]domain.JobReviewer, error) {
	var rs []domain.JobReviewer
	if err := s.db.Preload("User").Where("job_id = ?", jobID).Find(&rs).Error; err != nil {
		return nil, err
	}
	return rs, nil
}

// IsMember 判断用户是否为作业成员（设计师/PM/审查员）。
func IsMember(job *domain.Job, reviewers []domain.JobReviewer, userID string) bool {
	if job.DesignerID == userID || job.PMID == userID {
		return true
	}
	for _, r := range reviewers {
		if r.UserID == userID {
			return true
		}
	}
	return false
}

// ListVisible 返回用户能看到的作业（作为设计师/PM/审查员参与的）。
func (s *JobService) ListVisible(userID string) ([]domain.Job, error) {
	var jobs []domain.Job
	q := s.db.Preload("Designer").Preload("PM").Order("created_at DESC")
	err := q.Where(
		"designer_id = ? OR pm_id = ? OR id IN (?)",
		userID, userID,
		s.db.Model(&domain.JobReviewer{}).Select("job_id").Where("user_id = ?", userID),
	).Find(&jobs).Error
	if err != nil {
		return nil, err
	}
	return jobs, nil
}
