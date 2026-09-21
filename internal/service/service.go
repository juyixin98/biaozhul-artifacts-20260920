// Package service 实现 ProofCycle 的业务规则：作业、文件版本、审查意见与签核。
package service

import (
	"errors"

	"gorm.io/gorm"

	"proofcycle/internal/storage"
)

// 业务错误。HTTP 层据此映射状态码：
//   - ErrXxx 业务冲突/输入问题 -> 4xx
//   - 未包装的底层错误 -> 500
var (
	ErrNotFound              = errors.New("resource not found")
	ErrForbidden             = errors.New("forbidden")
	ErrValidation            = errors.New("validation error")
	ErrConflict              = errors.New("conflict")
	ErrTooManyReviewers      = errors.New("a job has at most 8 reviewers")
	ErrReviewerMissing       = errors.New("at least one reviewer is required")
	ErrDistinctMembers       = errors.New("designer, pm and reviewers must be distinct existing users")
	ErrUnknownUser           = errors.New("unknown user")
	ErrUnknownChecklist      = errors.New("unknown checklist")
	ErrNotDesigner           = errors.New("only the job designer can upload revisions")
	ErrVersionMissing        = errors.New("job has no file version yet")
	ErrStaleVersion          = errors.New("expected review version does not match current value")
	ErrNotAssignedReviewer   = errors.New("only assigned reviewers can submit reviews")
	ErrDesignerCannotApprove = errors.New("a designer cannot approve their own job")
	ErrNotPM                 = errors.New("only the assigned project manager can sign off")
	ErrIncompleteReview      = errors.New("reviews are incomplete: every checklist item must be answered")
	ErrUnprocessedItems      = errors.New("cannot approve: a reviewer has unprocessed checklist items")
	ErrFailedItems           = errors.New("cannot approve: one or more checklist items failed and need a new revision")
	ErrNotAllReviewers       = errors.New("cannot approve: not all assigned reviewers have completed their review")
	ErrDuplicateReviewer     = errors.New("reviewer list contains duplicates")
	ErrFailReasonRequired    = errors.New("fail result requires a reason")
	ErrUnknownSnapshotItem   = errors.New("review references unknown checklist item")
	ErrInvalidResult         = errors.New("result must be one of pass, fail, na")
	ErrAlreadyApproved       = errors.New("this version is already approved")
)

// Services 聚合所有业务服务，方便 HTTP 层使用。
type Services struct {
	DB       *gorm.DB
	Store    *storage.LocalStore
	User     *UserService
	Job      *JobService
	Version  *VersionService
	Review   *ReviewService
	Approval *ApprovalService
	Report   *ReportService
}

// New 组装服务。
func New(db *gorm.DB, store *storage.LocalStore, maxUpload int64) *Services {
	s := &Services{DB: db, Store: store}
	s.User = &UserService{db: db}
	s.Job = &JobService{db: db}
	s.Version = &VersionService{db: db, store: WrapStore(store)}
	s.Review = &ReviewService{db: db}
	s.Approval = &ApprovalService{db: db}
	s.Report = &ReportService{db: db}
	return s
}
