package integration

import (
	"bytes"
	"errors"
	"testing"

	"proofcycle/internal/domain"
	"proofcycle/internal/seed"
	"proofcycle/internal/service"
)

// TestReviewOnStaleVersionRejected 版本失效：v2 产生后不能再对 v1 写入意见，
// 但 v1 旧意见仍被保留（可读）。
func TestReviewOnStaleVersionRejected(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1, seed.Reviewer2})

	d1, err := e.upload(job.ID, seed.UserDesigner, "v1.pdf", validPDF(), "")
	if err != nil {
		t.Fatal(err)
	}
	ids1 := e.snapshotItemIDs(d1.Version.ID)
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, VersionNo: 1, Items: allPassInputs(ids1),
	}); err != nil {
		t.Fatalf("review v1: %v", err)
	}

	// 设计师提交 v2。
	d2, err := e.upload(job.ID, seed.UserDesigner, "v2.png", validPNG(), "")
	if err != nil {
		t.Fatal(err)
	}
	ids2 := e.snapshotItemIDs(d2.Version.ID)

	// 再对 v1 提交/更新意见 -> 明确冲突拒绝。
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer2, VersionNo: 1, Items: allPassInputs(ids1),
	}); !errors.Is(err, service.ErrStaleVersion) {
		t.Fatalf("submit review on stale v1: want ErrStaleVersion, got %v", err)
	}
	if _, err := e.svc.Review.Update(service.UpdateReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, VersionNo: 1, ExpectedVersion: 1,
		Items: allPassInputs(ids1),
	}); !errors.Is(err, service.ErrStaleVersion) {
		t.Fatalf("update review on stale v1: want ErrStaleVersion, got %v", err)
	}

	// v1 旧意见仍保留。
	h, err := e.svc.Version.History(job.ID, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(h.Reviews) != 1 || h.Reviews[0].Review.ReviewerID != seed.Reviewer1 {
		t.Fatalf("v1 old review must be retained, got %+v", h.Reviews)
	}

	// 当前 v2 可正常提交。
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, Items: allPassInputs(ids2),
	}); err != nil {
		t.Fatalf("review current v2: %v", err)
	}
}

// TestOnlyDesignerCanUpload 角色隔离：只有作业设计师能上传修订，
// 审查员、PM、非成员均被拒绝。
func TestOnlyDesignerCanUpload(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1})

	try := func(who string) error {
		_, err := e.svc.Version.UploadRevision(service.UploadInput{
			JobID: job.ID, UploaderID: who, FileName: "x.pdf",
			Reader: bytes.NewReader(validPDF()), MaxSize: 1 << 20,
		})
		return err
	}

	if err := try(seed.Reviewer1); !errors.Is(err, service.ErrNotDesigner) {
		t.Fatalf("reviewer upload: want ErrNotDesigner, got %v", err)
	}
	if err := try(seed.UserPM); !errors.Is(err, service.ErrNotDesigner) {
		t.Fatalf("pm upload: want ErrNotDesigner, got %v", err)
	}
	if err := try(seed.Reviewer2); !errors.Is(err, service.ErrNotDesigner) {
		t.Fatalf("outsider upload: want ErrNotDesigner, got %v", err)
	}
	// 设计师本人成功。
	if d, err := e.upload(job.ID, seed.UserDesigner, "v1.pdf", validPDF(), ""); err != nil {
		t.Fatalf("designer upload should succeed: %v", err)
	} else if d.VersionNo != 1 {
		t.Fatalf("version no = %d", d.VersionNo)
	}
}

// TestNAPassed 不适用(na) 与通过一样允许签核。
func TestNAPassed(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)
	naInputs := allPassInputs(ids)
	naInputs[0].Result = domain.ResultNotApplicable
	for _, r := range []string{seed.Reviewer1, seed.Reviewer2} {
		if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
			JobID: job.ID, Reviewer: r, Items: naInputs,
		}); err != nil {
			t.Fatalf("review with na from %s: %v", r, err)
		}
	}
	if _, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 0); err != nil {
		t.Fatalf("na items must not block approval: %v", err)
	}
}
