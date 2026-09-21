package service

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"gorm.io/gorm"

	"proofcycle/internal/domain"
	"proofcycle/internal/util"
)

// ReviewService 审查意见的提交与更新。
type ReviewService struct {
	db *gorm.DB
}

// ItemInput 单个检查项的结论。
type ItemInput struct {
	SnapshotItemID string
	Result         domain.ItemResult
	FailReason     string
}

// SubmitReviewInput 提交一份意见。
type SubmitReviewInput struct {
	JobID     string
	VersionNo int    // 0 表示当前版本
	Reviewer  string // 调用者用户 ID
	Items     []ItemInput
}

// Submit 针对指定版本提交或重新提交本人意见。
//
// 规则：
//   - 仅作业指定审查员可提交，且只能提交自己的意见；
//   - 必须对该版本清单快照中的每一项给出 pass/fail/na，fail 必填原因；
//   - 每个审查员每个版本至多一份意见（唯一索引 + 先查后写）；
//   - 作业级 FOR UPDATE 行锁与上传修订/签核串行。
func (s *ReviewService) Submit(in SubmitReviewInput) (*domain.Review, error) {
	var result *domain.Review
	txErr := s.db.Transaction(func(tx *gorm.DB) error {
		job, err := lockJob(tx, in.JobID)
		if err != nil {
			return err
		}
		assigned, err := assignedReviewerIDs(tx, job.ID, in.Reviewer)
		if err != nil {
			return err
		}
		if !assigned {
			return ErrNotAssignedReviewer
		}

		version, err := resolveVersion(tx, job, in.VersionNo)
		if err != nil {
			return err
		}
		if err := ensureCurrentVersion(job, version); err != nil {
			return err
		}
		snapItems, err := validateItems(tx, version.SnapshotID, in.Items)
		if err != nil {
			return err
		}

		// 已存在意见：整体替换条目，行版本递增。
		var existing domain.Review
		err = tx.First(&existing, "version_id = ? AND reviewer_id = ?", version.ID, in.Reviewer).Error
		if err == nil {
			if err := tx.Where("review_id = ?", existing.ID).Delete(&domain.ReviewItem{}).Error; err != nil {
				return err
			}
			now := time.Now().UTC()
			if err := tx.Model(&existing).Updates(map[string]any{
				"row_version":  gorm.Expr("row_version + 1"),
				"updated_at":   now,
				"submitted_at": now,
			}).Error; err != nil {
				return err
			}
			items, err := buildReviewItems(existing.ID, snapItems, in.Items)
			if err != nil {
				return err
			}
			if err := tx.Create(&items).Error; err != nil {
				return err
			}
			existing.RowVersion++
			result = &existing
			return nil
		}
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}

		review := domain.Review{
			ID:          util.NewID(),
			VersionID:   version.ID,
			ReviewerID:  in.Reviewer,
			RowVersion:  1,
			SubmittedAt: time.Now().UTC(),
			UpdatedAt:   time.Now().UTC(),
		}
		if err := tx.Create(&review).Error; err != nil {
			return err
		}
		items, err := buildReviewItems(review.ID, snapItems, in.Items)
		if err != nil {
			return err
		}
		if err := tx.Create(&items).Error; err != nil {
			return err
		}
		result = &review
		return nil
	})
	if txErr != nil {
		return nil, txErr
	}
	return result, nil
}

// UpdateReviewInput 携带预期版本的增量/整体更新。
type UpdateReviewInput struct {
	JobID           string
	VersionNo       int // 0 表示当前版本
	Reviewer        string
	ExpectedVersion int // 调用方上次读到的 review.row_version
	Items           []ItemInput
}

// Update 按乐观锁更新意见：row_version 不匹配则明确返回 ErrStaleVersion(409)。
// 适用于“两个客户端同时修改意见”的冲突拒绝。
func (s *ReviewService) Update(in UpdateReviewInput) (*domain.Review, error) {
	var result *domain.Review
	txErr := s.db.Transaction(func(tx *gorm.DB) error {
		job, err := lockJob(tx, in.JobID)
		if err != nil {
			return err
		}
		assigned, err := assignedReviewerIDs(tx, job.ID, in.Reviewer)
		if err != nil {
			return err
		}
		if !assigned {
			return ErrNotAssignedReviewer
		}
		version, err := resolveVersion(tx, job, in.VersionNo)
		if err != nil {
			return err
		}
		if err := ensureCurrentVersion(job, version); err != nil {
			return err
		}
		snapItems, err := validateItems(tx, version.SnapshotID, in.Items)
		if err != nil {
			return err
		}

		var review domain.Review
		err = tx.First(&review, "version_id = ? AND reviewer_id = ?", version.ID, in.Reviewer).Error
		if err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return ErrNotFound // 尚未提交意见：请使用 submit
			}
			return err
		}
		if review.RowVersion != in.ExpectedVersion {
			return fmt.Errorf("%w: expected row_version=%d but server has %d",
				ErrStaleVersion, in.ExpectedVersion, review.RowVersion)
		}

		if err := tx.Where("review_id = ?", review.ID).Delete(&domain.ReviewItem{}).Error; err != nil {
			return err
		}
		items, err := buildReviewItems(review.ID, snapItems, in.Items)
		if err != nil {
			return err
		}
		if err := tx.Create(&items).Error; err != nil {
			return err
		}
		now := time.Now().UTC()
		res := tx.Model(&domain.Review{}).
			Where("id = ? AND row_version = ?", review.ID, in.ExpectedVersion).
			Updates(map[string]any{
				"row_version":  in.ExpectedVersion + 1,
				"updated_at":   now,
				"submitted_at": now,
			})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			// 极端竞争下行版本已被改写，明确冲突。
			return ErrStaleVersion
		}
		review.RowVersion = in.ExpectedVersion + 1
		result = &review
		return nil
	})
	if txErr != nil {
		return nil, txErr
	}
	return result, nil
}

// Get 取某审查员在某版本上的意见（不存在返回 ErrNotFound）。
func (s *ReviewService) Get(versionID, reviewerID string) (*domain.Review, []domain.ReviewItem, error) {
	var r domain.Review
	if err := s.db.First(&r, "version_id = ? AND reviewer_id = ?", versionID, reviewerID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, nil, ErrNotFound
		}
		return nil, nil, err
	}
	var items []domain.ReviewItem
	if err := s.db.Where("review_id = ?", r.ID).Find(&items).Error; err != nil {
		return nil, nil, err
	}
	return &r, items, nil
}

// assignedReviewerIDs 检查 userID 是否为作业指定审查员。
func assignedReviewerIDs(tx *gorm.DB, jobID, userID string) (bool, error) {
	var n int64
	if err := tx.Model(&domain.JobReviewer{}).
		Where("job_id = ? AND user_id = ?", jobID, userID).Count(&n).Error; err != nil {
		return false, err
	}
	return n == 1, nil
}

// resolveVersion 取作业当前版本（或指定版本号）。
func resolveVersion(tx *gorm.DB, job *domain.Job, versionNo int) (*domain.FileVersion, error) {
	if versionNo <= 0 {
		versionNo = job.CurrentVersionNo
	}
	if versionNo == 0 {
		return nil, ErrVersionMissing
	}
	var v domain.FileVersion
	if err := tx.First(&v, "job_id = ? AND version_no = ?", job.ID, versionNo).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &v, nil
}

// ensureCurrentVersion 校验目标版本仍是作业当前版本。
// 新修订产生后，旧版本进入只读历史：意见保留但不能再被写入，
// 避免针对已失效版本提交/修改意见。
func ensureCurrentVersion(job *domain.Job, v *domain.FileVersion) error {
	if v.VersionNo != job.CurrentVersionNo {
		return fmt.Errorf("%w: 版本 v%d 已被 v%d 取代，不能再对其写入意见",
			ErrStaleVersion, v.VersionNo, job.CurrentVersionNo)
	}
	return nil
}

// validateItems 校验提交覆盖快照的每一项，结果合法且 fail 有原因；返回以快照项 ID 为键的映射。
func validateItems(tx *gorm.DB, snapshotID string, in []ItemInput) (map[string]domain.ChecklistSnapshotItem, error) {
	if len(in) == 0 {
		return nil, ErrIncompleteReview
	}
	var snapItems []domain.ChecklistSnapshotItem
	if err := tx.Where("snapshot_id = ?", snapshotID).Order("order_no").Find(&snapItems).Error; err != nil {
		return nil, err
	}
	byID := make(map[string]domain.ChecklistSnapshotItem, len(snapItems))
	for _, it := range snapItems {
		byID[it.ID] = it
	}
	seen := make(map[string]bool, len(in))
	for _, x := range in {
		if _, ok := byID[x.SnapshotItemID]; !ok {
			return nil, fmt.Errorf("%w: %s", ErrUnknownSnapshotItem, x.SnapshotItemID)
		}
		if seen[x.SnapshotItemID] {
			return nil, fmt.Errorf("%w: duplicated item %s", ErrValidation, x.SnapshotItemID)
		}
		seen[x.SnapshotItemID] = true
		if !x.Result.Valid() {
			return nil, fmt.Errorf("%w: %s", ErrInvalidResult, x.Result)
		}
		if x.Result == domain.ResultFail && strings.TrimSpace(x.FailReason) == "" {
			return nil, fmt.Errorf("%w: item %s", ErrFailReasonRequired, byID[x.SnapshotItemID].Code)
		}
	}
	if len(seen) != len(byID) {
		return nil, ErrIncompleteReview
	}
	return byID, nil
}

func buildReviewItems(reviewID string, snap map[string]domain.ChecklistSnapshotItem, in []ItemInput) ([]domain.ReviewItem, error) {
	items := make([]domain.ReviewItem, 0, len(in))
	for _, x := range in {
		items = append(items, domain.ReviewItem{
			ID:             util.NewID(),
			ReviewID:       reviewID,
			SnapshotItemID: x.SnapshotItemID,
			Result:         x.Result,
			FailReason:     strings.TrimSpace(x.FailReason),
		})
	}
	return items, nil
}
