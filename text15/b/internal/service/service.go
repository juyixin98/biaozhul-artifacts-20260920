package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"gorm.io/gorm"

	"proofcycle/internal/model"
	"proofcycle/internal/storage"
)

const maxReviewers = 8

// Service contains the business logic. Every write touching a job takes a
// FOR UPDATE row lock on that job first, serializing upload / opinion /
// sign-off transactions and removing any cross-operation race.
type Service struct {
	db      *gorm.DB
	store   *storage.Storage
	maxSize int64
}

func New(db *gorm.DB, store *storage.Storage, maxUploadSize int64) *Service {
	return &Service{db: db, store: store, maxSize: maxUploadSize}
}

// MaxUploadSize exposes the configured upload cap.
func (s *Service) MaxUploadSize() int64 { return s.maxSize }

// ---- users ----

// CreateUser registers a principal and returns its static API token exactly
// once. Authenticated callers only in production (see middleware); the seed
// path uses the same code.
func (s *Service) CreateUser(ctx context.Context, in CreateUserInput) (*model.User, error) {
	name := strings.TrimSpace(in.Name)
	if name == "" || len(name) > 100 {
		return nil, errBadRequest("name is required (max 100 chars)")
	}
	switch in.Role {
	case model.RoleDesigner, model.RolePM, model.RoleReviewer:
	default:
		return nil, errBadRequest("role must be designer, pm or reviewer")
	}
	token, err := randomToken()
	if err != nil {
		return nil, err
	}
	u := &model.User{Name: name, Role: in.Role, APIToken: token, CreatedAt: time.Now().UTC()}
	if err := s.db.WithContext(ctx).Create(u).Error; err != nil {
		if isDuplicate(err) {
			return nil, errConflict("user already exists")
		}
		return nil, err
	}
	return u, nil
}

func (s *Service) UserByToken(ctx context.Context, token string) (*model.User, error) {
	var u model.User
	err := s.db.WithContext(ctx).Where("api_token = ?", token).First(&u).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, errUnauthorized("invalid API token")
	}
	if err != nil {
		return nil, err
	}
	return &u, nil
}

func (s *Service) loadUsers(ctx context.Context, tx *gorm.DB, ids []int64) (map[int64]model.User, error) {
	out := map[int64]model.User{}
	if len(ids) == 0 {
		return out, nil
	}
	var us []model.User
	if err := tx.WithContext(ctx).Where("id IN ?", ids).Find(&us).Error; err != nil {
		return nil, err
	}
	for _, u := range us {
		out[u.ID] = u
	}
	return out, nil
}

// ---- jobs ----

// CreateJob creates a job owned by the calling designer, with its immutable
// reviewer assignment (1..8 reviewers) and checklist template.
func (s *Service) CreateJob(ctx context.Context, caller *model.User, in CreateJobInput) (*model.Job, error) {
	if caller.Role != model.RoleDesigner {
		return nil, errForbidden("only designers can create jobs")
	}
	name := strings.TrimSpace(in.Name)
	if name == "" || len(name) > 200 {
		return nil, errBadRequest("job name is required (max 200 chars)")
	}
	if in.PMID == caller.ID {
		return nil, errBadRequest("the designer cannot also be the project manager")
	}
	if len(in.ReviewerIDs) == 0 || len(in.ReviewerIDs) > maxReviewers {
		return nil, errBadRequest(fmt.Sprintf("between 1 and %d reviewers are required", maxReviewers))
	}
	seen := map[int64]struct{}{}
	for _, id := range in.ReviewerIDs {
		if id <= 0 {
			return nil, errBadRequest("reviewer id must be positive")
		}
		if id == caller.ID {
			return nil, errBadRequest("the designer cannot be a reviewer of the job")
		}
		if id == in.PMID {
			return nil, errBadRequest("the project manager cannot also be a reviewer")
		}
		if _, dup := seen[id]; dup {
			return nil, errBadRequest("duplicate reviewer assignment")
		}
		seen[id] = struct{}{}
	}
	if len(in.Checklist) == 0 {
		return nil, errBadRequest("at least one checklist item is required")
	}
	codes := map[string]struct{}{}
	for i, c := range in.Checklist {
		c.Code = strings.TrimSpace(c.Code)
		c.Description = strings.TrimSpace(c.Description)
		if c.Code == "" || len(c.Code) > 40 || c.Description == "" || len(c.Description) > 500 {
			return nil, errBadRequest(fmt.Sprintf("checklist item %d needs code (max 40) and description (max 500)", i+1))
		}
		if _, dup := codes[c.Code]; dup {
			return nil, errBadRequest("duplicate checklist code: " + c.Code)
		}
		codes[c.Code] = struct{}{}
	}

	ids := append([]int64{caller.ID, in.PMID}, in.ReviewerIDs...)
	users, err := s.loadUsers(ctx, s.db, ids)
	if err != nil {
		return nil, err
	}
	pm, ok := users[in.PMID]
	if !ok {
		return nil, errBadRequest("project manager does not exist")
	}
	if pm.Role != model.RolePM {
		return nil, errBadRequest("pm_id must reference a project manager")
	}
	for _, rid := range in.ReviewerIDs {
		u, ok := users[rid]
		if !ok {
			return nil, errBadRequest("reviewer does not exist")
		}
		if u.Role != model.RoleReviewer {
			return nil, errBadRequest("reviewer id must reference a reviewer user")
		}
	}

	now := time.Now().UTC()
	job := &model.Job{
		Name:       name,
		DesignerID: caller.ID,
		PMID:       in.PMID,
		Status:     model.JobStatusInReview,
		CreatedAt:  now,
		UpdatedAt:  now,
	}
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(job).Error; err != nil {
			if isDuplicate(err) {
				return errConflict("a job with this name already exists for the designer")
			}
			return err
		}
		for _, rid := range in.ReviewerIDs {
			if err := tx.Create(&model.JobReviewer{JobID: job.ID, ReviewerID: rid, CreatedAt: now}).Error; err != nil {
				return err
			}
		}
		for i, c := range in.Checklist {
			if err := tx.Create(&model.JobChecklistItem{
				JobID: job.ID, ItemOrder: i + 1, Code: c.Code, Description: c.Description,
			}).Error; err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return job, nil
}

// ---- queries ----

func (s *Service) ListJobs(ctx context.Context, caller *model.User) ([]model.Job, error) {
	q := s.db.WithContext(ctx).Model(&model.Job{})
	switch caller.Role {
	case model.RoleDesigner:
		q = q.Where("designer_id = ?", caller.ID)
	case model.RolePM:
		q = q.Where("pm_id = ?", caller.ID)
	default:
		q = q.Joins("JOIN job_reviewers jr ON jr.job_id = jobs.id AND jr.reviewer_id = ?", caller.ID)
	}
	var jobs []model.Job
	if err := q.Order("id DESC").Find(&jobs).Error; err != nil {
		return nil, err
	}
	return jobs, nil
}

// lockJob loads the job and takes a row lock; must run inside a transaction.
func lockJob(ctx context.Context, tx *gorm.DB, jobID int64) (*model.Job, error) {
	var job model.Job
	err := tx.WithContext(ctx).Clauses(clauseForUpdate()).Where("id = ?", jobID).First(&job).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, errNotFound("job not found")
	}
	if err != nil {
		return nil, err
	}
	return &job, nil
}

func (s *Service) loadJob(ctx context.Context, jobID int64) (*model.Job, error) {
	var job model.Job
	err := s.db.WithContext(ctx).Where("id = ?", jobID).First(&job).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, errNotFound("job not found")
	}
	if err != nil {
		return nil, err
	}
	return &job, nil
}

// authorizeParticipant loads a job and verifies the caller is one of its
// designer, PM or designated reviewers. Reports/history never leak past it.
func (s *Service) authorizeParticipant(ctx context.Context, caller *model.User, jobID int64) (*model.Job, map[int64]model.User, error) {
	job, err := s.loadJob(ctx, jobID)
	if err != nil {
		return nil, nil, err
	}
	var reviewers []model.JobReviewer
	if err := s.db.WithContext(ctx).Where("job_id = ?", jobID).Find(&reviewers).Error; err != nil {
		return nil, nil, err
	}
	isR := false
	ids := []int64{job.DesignerID, job.PMID}
	for _, r := range reviewers {
		if r.ReviewerID == caller.ID {
			isR = true
		}
		ids = append(ids, r.ReviewerID)
	}
	if caller.ID != job.DesignerID && caller.ID != job.PMID && !isR {
		return nil, nil, errForbidden("you are not assigned to this job")
	}
	users, err := s.loadUsers(ctx, s.db, ids)
	if err != nil {
		return nil, nil, err
	}
	return job, users, nil
}

func randomToken() (string, error) {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func isDuplicate(err error) bool {
	return err != nil && strings.Contains(err.Error(), "Duplicate entry")
}
