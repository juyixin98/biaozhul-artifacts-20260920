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

// ApprovalService 最终签核。
type ApprovalService struct {
	db *gorm.DB
}

// SignOff 由项目经理对作业“当前版本”最终签核。
//
// 必须同时满足（全部在 FOR UPDATE 作业行锁内判定）：
//  1. 调用者是该作业 PM；设计师不能批准自己的作业（由身份天然保证，并显式拦截）；
//  2. 目标版本必须是当前版本——旧版本即便曾满足条件也不能据此批准；
//  3. 所有指定审查员都已提交意见，且每份意见覆盖快照全部条目（无未处理项）；
//  4. 没有任何 fail 结论（na 允许）；
//  5. 该版本尚无签核（唯一索引兜底，防并发双签）。
//
// 并发的“签核 / 修改意见 / 提交新修订”都在同一行锁上串行，任一检查都无法被绕过。
type SignOffResult struct {
	Approval *domain.Approval
	Basis    []string
}

func (s *ApprovalService) SignOff(jobID, approverID string, versionNo int) (*SignOffResult, error) {
	var out *SignOffResult
	txErr := s.db.Transaction(func(tx *gorm.DB) error {
		job, err := lockJob(tx, jobID)
		if err != nil {
			return err
		}
		if job.DesignerID == approverID {
			return ErrDesignerCannotApprove
		}
		if job.PMID != approverID {
			return ErrNotPM
		}
		if versionNo <= 0 {
			versionNo = job.CurrentVersionNo
		}
		if versionNo != job.CurrentVersionNo {
			// 旧版本意见保留，但不能作为新版本批准依据；旧版本也不能再签。
			return fmt.Errorf("%w: version %d is not current (%d)", ErrConflict, versionNo, job.CurrentVersionNo)
		}
		version, err := resolveVersion(tx, job, versionNo)
		if err != nil {
			return err
		}

		// 已有签核：幂等拒绝，杜绝并发双签。
		var existing domain.Approval
		err = tx.First(&existing, "version_id = ?", version.ID).Error
		if err == nil {
			return ErrAlreadyApproved
		} else if !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}

		// 指定审查员集合。
		var jobReviewers []domain.JobReviewer
		if err := tx.Where("job_id = ?", job.ID).Order("created_at").Find(&jobReviewers).Error; err != nil {
			return err
		}
		if len(jobReviewers) == 0 {
			return ErrNotAllReviewers
		}

		// 快照项集合（用于判定未处理项）。
		var snapItems []domain.ChecklistSnapshotItem
		if err := tx.Where("snapshot_id = ?", version.SnapshotID).Order("order_no").Find(&snapItems).Error; err != nil {
			return err
		}

		var reviews []domain.Review
		if err := tx.Where("version_id = ?", version.ID).Find(&reviews).Error; err != nil {
			return err
		}
		byReviewer := make(map[string]domain.Review, len(reviews))
		for _, r := range reviews {
			byReviewer[r.ReviewerID] = r
		}

		basis := []string{
			fmt.Sprintf("签核版本 v%d，文件 SHA-256=%s", version.VersionNo, version.SHA256),
		}
		for _, jr := range jobReviewers {
			rev, ok := byReviewer[jr.UserID]
			if !ok {
				return fmt.Errorf("%w: 审查员 %s 尚未提交意见", ErrNotAllReviewers, jr.UserID)
			}
			var items []domain.ReviewItem
			if err := tx.Where("review_id = ?", rev.ID).Find(&items).Error; err != nil {
				return err
			}
			if len(items) != len(snapItems) {
				return fmt.Errorf("%w: 审查员 %s 有未处理检查项（%d/%d）",
					ErrUnprocessedItems, jr.UserID, len(items), len(snapItems))
			}
			var fails []string
			itemBySnap := make(map[string]domain.ReviewItem, len(items))
			for _, it := range items {
				itemBySnap[it.SnapshotItemID] = it
				if it.Result == domain.ResultFail {
					fails = append(fails, it.FailReason)
				}
			}
			// 双保险：条目数相同也必须覆盖到每一个快照项。
			if len(itemBySnap) != len(snapItems) {
				return fmt.Errorf("%w: 审查员 %s 有未处理检查项", ErrUnprocessedItems, jr.UserID)
			}
			if len(fails) > 0 {
				return fmt.Errorf("%w: 审查员 %s 存在失败项：%s",
					ErrFailedItems, jr.UserID, strings.Join(fails, "；"))
			}
			basis = append(basis, fmt.Sprintf("审查员 %s 已完成全部 %d 项，无失败项（意见版本 row_version=%d）",
				jr.UserID, len(snapItems), rev.RowVersion))
		}
		basis = append(basis, fmt.Sprintf("共 %d 名指定审查员全部完成，检查项无 fail，允许签核", len(jobReviewers)))

		approval := &domain.Approval{
			ID:         util.NewID(),
			JobID:      job.ID,
			VersionID:  version.ID,
			ApproverID: approverID,
			Basis:      strings.Join(basis, "\n"),
			CreatedAt:  time.Now().UTC(),
		}
		// 唯一索引 (job_id, version_id) 在并发下兜底：第二个插入必失败。
		if err := tx.Create(approval).Error; err != nil {
			if isDuplicateKey(err) {
				return ErrAlreadyApproved
			}
			return err
		}
		if err := tx.Model(&domain.Job{}).Where("id = ?", job.ID).
			Update("status", domain.StatusApproved).Error; err != nil {
			return err
		}
		out = &SignOffResult{Approval: approval, Basis: basis}
		return nil
	})
	if txErr != nil {
		return nil, txErr
	}
	return out, nil
}
