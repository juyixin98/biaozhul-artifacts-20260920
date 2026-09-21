package integration

import (
	"errors"
	"sync"
	"testing"

	"proofcycle/internal/domain"
	"proofcycle/internal/seed"
	"proofcycle/internal/service"
)

// twoReviewerEnv 搭建：作业含 2 名审查员，已上传 v1。返回快照项 ID。
func twoReviewerEnv(t *testing.T) (*testEnv, *domain.Job, []string) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1, seed.Reviewer2})
	d, err := e.upload(job.ID, seed.UserDesigner, "v1.pdf", validPDF(), "")
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	return e, job, e.snapshotItemIDs(d.Version.ID)
}

func TestFailItemRequiresReasonAndBlocksApproval(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)

	// fail 不填原因 -> 拒绝。
	items := allPassInputs(ids)
	items[0].Result = domain.ResultFail
	items[0].FailReason = ""
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, Items: items,
	}); !errors.Is(err, service.ErrFailReasonRequired) {
		t.Fatalf("fail without reason: want ErrFailReasonRequired, got %v", err)
	}

	// 填了原因可提交。
	items[0].FailReason = "色差 ΔE 超标"
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, Items: items,
	}); err != nil {
		t.Fatalf("fail with reason: %v", err)
	}
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer2, Items: allPassInputs(ids),
	}); err != nil {
		t.Fatalf("reviewer2: %v", err)
	}
	if _, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 0); !errors.Is(err, service.ErrFailedItems) {
		t.Fatalf("approval with fail: want ErrFailedItems, got %v", err)
	}
}

func TestIncompleteReviewsBlockApproval(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)

	// 只回答部分检查项。
	partial := []service.ItemInput{{SnapshotItemID: ids[0], Result: domain.ResultPass}}
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, Items: partial,
	}); !errors.Is(err, service.ErrIncompleteReview) {
		t.Fatalf("partial items: want ErrIncompleteReview, got %v", err)
	}

	// 一名审查员完全未提交 -> 审查员未齐，拦截。
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, Items: allPassInputs(ids),
	}); err != nil {
		t.Fatalf("reviewer1: %v", err)
	}
	if _, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 0); !errors.Is(err, service.ErrNotAllReviewers) {
		t.Fatalf("missing reviewer: want ErrNotAllReviewers, got %v", err)
	}
}

func TestHappyPathApprovalAndIdempotent(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)
	for _, r := range []string{seed.Reviewer1, seed.Reviewer2} {
		if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
			JobID: job.ID, Reviewer: r, Items: allPassInputs(ids),
		}); err != nil {
			t.Fatalf("review %s: %v", r, err)
		}
	}
	out, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 0)
	if err != nil {
		t.Fatalf("approve: %v", err)
	}
	if out.Approval.Basis == "" || len(out.Basis) < 2 {
		t.Fatalf("approval basis must be recorded")
	}
	// 重复签核：明确拒绝，且状态不被破坏。
	if _, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 0); !errors.Is(err, service.ErrAlreadyApproved) {
		t.Fatalf("duplicate approval: want ErrAlreadyApproved, got %v", err)
	}
}

func TestConcurrentSignOffOnlyOneSucceeds(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)
	for _, r := range []string{seed.Reviewer1, seed.Reviewer2} {
		if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
			JobID: job.ID, Reviewer: r, Items: allPassInputs(ids),
		}); err != nil {
			t.Fatalf("review %s: %v", r, err)
		}
	}

	const n = 8
	var wg sync.WaitGroup
	var mu sync.Mutex
	ok, conflict := 0, 0
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			_, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 0)
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				ok++
			case errors.Is(err, service.ErrAlreadyApproved):
				conflict++
			default:
				t.Errorf("unexpected sign-off error: %v", err)
			}
		}()
	}
	wg.Wait()
	if ok != 1 || conflict != n-1 {
		t.Fatalf("want exactly 1 success and %d conflicts, got ok=%d conflict=%d", n-1, ok, conflict)
	}
	var ap int64
	e.db.Model(&domain.Approval{}).Where("job_id = ?", job.ID).Count(&ap)
	if ap != 1 {
		t.Fatalf("exactly one approval row expected, got %d", ap)
	}
}

func TestReviewerCanOnlySubmitOwnReview(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)

	// 未分配的审查员（3）不能提交。
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer3, Items: allPassInputs(ids),
	}); !errors.Is(err, service.ErrNotAssignedReviewer) {
		t.Fatalf("unassigned reviewer: want ErrNotAssignedReviewer, got %v", err)
	}
	// 设计师、PM 也不能充当审查员。
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.UserDesigner, Items: allPassInputs(ids),
	}); !errors.Is(err, service.ErrNotAssignedReviewer) {
		t.Fatalf("designer as reviewer: want ErrNotAssignedReviewer, got %v", err)
	}
}

func TestDesignerCannotApproveOwnJob(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)
	for _, r := range []string{seed.Reviewer1, seed.Reviewer2} {
		_, _ = e.svc.Review.Submit(service.SubmitReviewInput{
			JobID: job.ID, Reviewer: r, Items: allPassInputs(ids),
		})
	}
	// 设计师尝试签核自己的作业。
	if _, err := e.svc.Approval.SignOff(job.ID, seed.UserDesigner, 0); !errors.Is(err, service.ErrDesignerCannotApprove) {
		t.Fatalf("designer self-approve: want ErrDesignerCannotApprove, got %v", err)
	}
	// 非本作业 PM 也不能签（用另一个 PM 角色用户：种子里只有一个 PM，
	// 这里临时插入另一个 PM）。
	other := &domain.User{ID: "u-pm-other", Name: "其他PM", Role: domain.RolePM}
	if err := e.db.Create(other).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := e.svc.Approval.SignOff(job.ID, other.ID, 0); !errors.Is(err, service.ErrNotPM) {
		t.Fatalf("other pm: want ErrNotPM, got %v", err)
	}
}

func TestOptimisticLockOnReviewUpdate(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, Items: allPassInputs(ids),
	}); err != nil {
		t.Fatal(err)
	}
	// 先用期望版本 1 更新成功 -> row_version 变为 2。
	if _, err := e.svc.Review.Update(service.UpdateReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, ExpectedVersion: 1,
		Items: allPassInputs(ids),
	}); err != nil {
		t.Fatalf("first update: %v", err)
	}
	// 另一个客户端仍拿着旧版本 1 -> 明确冲突。
	_, err := e.svc.Review.Update(service.UpdateReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, ExpectedVersion: 1,
		Items: allPassInputs(ids),
	})
	if !errors.Is(err, service.ErrStaleVersion) {
		t.Fatalf("stale update: want ErrStaleVersion, got %v", err)
	}
	// 用最新版本 2 可再次更新。
	if _, err := e.svc.Review.Update(service.UpdateReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, ExpectedVersion: 2,
		Items: allPassInputs(ids),
	}); err != nil {
		t.Fatalf("fresh update: %v", err)
	}
}
