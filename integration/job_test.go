package integration

import (
	"errors"
	"strings"
	"testing"

	"proofcycle/internal/domain"
	"proofcycle/internal/seed"
	"proofcycle/internal/service"
)

func TestCreateJobValidation(t *testing.T) {
	e := newTestEnv(t)

	// 0 名审查员。
	if _, _, err := e.svc.Job.Create(service.CreateJobInput{
		Name: "j", DesignerID: seed.UserDesigner, PMID: seed.UserPM,
		ReviewerIDs: nil, ChecklistID: seed.DefaultChecklist,
	}); !errors.Is(err, service.ErrReviewerMissing) {
		t.Fatalf("no reviewers: want ErrReviewerMissing, got %v", err)
	}

	// 超过 8 名。
	nine := []string{seed.Reviewer1, seed.Reviewer2, seed.Reviewer3, seed.Reviewer4,
		seed.Reviewer5, seed.Reviewer6, seed.Reviewer7, seed.Reviewer8, seed.UserDesigner}
	if _, _, err := e.svc.Job.Create(service.CreateJobInput{
		Name: "j", DesignerID: seed.UserDesigner, PMID: seed.UserPM,
		ReviewerIDs: nine, ChecklistID: seed.DefaultChecklist,
	}); err == nil {
		t.Fatalf("9 reviewers must be rejected (max 8)")
	}

	// 重复审查员。
	if _, _, err := e.svc.Job.Create(service.CreateJobInput{
		Name: "j", DesignerID: seed.UserDesigner, PMID: seed.UserPM,
		ReviewerIDs: []string{seed.Reviewer1, seed.Reviewer1},
		ChecklistID: seed.DefaultChecklist,
	}); !errors.Is(err, service.ErrDuplicateReviewer) {
		t.Fatalf("duplicate reviewer: want ErrDuplicateReviewer, got %v", err)
	}

	// 设计师与 PM 相同。
	if _, _, err := e.svc.Job.Create(service.CreateJobInput{
		Name: "j", DesignerID: seed.UserDesigner, PMID: seed.UserDesigner,
		ReviewerIDs: []string{seed.Reviewer1}, ChecklistID: seed.DefaultChecklist,
	}); err != nil {
		// ErrUnknownUser 也可能（PM 角色不对）或 ErrDistinctMembers；二者都可接受为拒绝，
		// 这里只断言“被拒绝”。
	} else {
		t.Fatalf("same designer/pm must be rejected")
	}

	// 8 名上限内成功。
	eight := []string{seed.Reviewer1, seed.Reviewer2, seed.Reviewer3, seed.Reviewer4,
		seed.Reviewer5, seed.Reviewer6, seed.Reviewer7, seed.Reviewer8}
	job, _, err := e.svc.Job.Create(service.CreateJobInput{
		Name: "8 人作业", DesignerID: seed.UserDesigner, PMID: seed.UserPM,
		ReviewerIDs: eight, ChecklistID: seed.DefaultChecklist,
	})
	if err != nil {
		t.Fatalf("8 reviewers should succeed: %v", err)
	}
	rs, err := e.svc.Job.Reviewers(job.ID)
	if err != nil || len(rs) != 8 {
		t.Fatalf("reviewers = %d, err=%v", len(rs), err)
	}
}

func TestListVisibleIsolatesJobs(t *testing.T) {
	e := newTestEnv(t)
	e.mustCreateJob([]string{seed.Reviewer1})

	// 审查员 2 与该作业无关，看不到它。
	vis2, err := e.svc.Job.ListVisible(seed.Reviewer2)
	if err != nil {
		t.Fatal(err)
	}
	if len(vis2) != 0 {
		t.Fatalf("outsider must see no jobs, got %d", len(vis2))
	}
	vis1, err := e.svc.Job.ListVisible(seed.Reviewer1)
	if err != nil {
		t.Fatal(err)
	}
	if len(vis1) != 1 {
		t.Fatalf("assigned reviewer must see 1 job, got %d", len(vis1))
	}
	visDesigner, err := e.svc.Job.ListVisible(seed.UserDesigner)
	if err != nil {
		t.Fatal(err)
	}
	if len(visDesigner) != 1 {
		t.Fatalf("designer must see their job, got %d", len(visDesigner))
	}
}

func TestHistoryAndReportByVersion(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)
	fail := allPassInputs(ids)
	fail[3].Result = domain.ResultFail
	fail[3].FailReason = "条码扫不出"

	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, Items: fail,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer2, Items: allPassInputs(ids),
	}); err != nil {
		t.Fatal(err)
	}

	h, err := e.svc.Version.History(job.ID, 1)
	if err != nil {
		t.Fatalf("history: %v", err)
	}
	if len(h.Items) != 8 || len(h.Reviews) != 2 || h.Approval != nil {
		t.Fatalf("history content wrong: items=%d reviews=%d approval=%v",
			len(h.Items), len(h.Reviews), h.Approval)
	}

	rep, err := e.svc.Report.Build(job.ID, 1)
	if err != nil {
		t.Fatalf("report: %v", err)
	}
	if rep.File.SHA256 == "" || rep.File.SizeBytes == 0 {
		t.Fatalf("report must include file digest/size")
	}
	if rep.Checklist.TotalItems != 8 {
		t.Fatalf("checklist items = %d", rep.Checklist.TotalItems)
	}
	if rep.ReviewStatus != "has_failed_items" {
		t.Fatalf("status = %s, want has_failed_items", rep.ReviewStatus)
	}
	md := rep.RenderMarkdown()
	if !strings.Contains(md, "文件摘要") || !strings.Contains(md, "检查清单") {
		t.Fatalf("markdown report missing sections")
	}
	if !strings.Contains(md, "条码扫不出") {
		t.Fatalf("markdown report must include fail reason")
	}

	// 不存在的版本。
	if _, err := e.svc.Report.Build(job.ID, 99); !errors.Is(err, service.ErrNotFound) {
		t.Fatalf("missing version report: want ErrNotFound, got %v", err)
	}
}

func TestReportAfterApprovalContainsBasis(t *testing.T) {
	e, job, ids := twoReviewerEnv(t)
	for _, r := range []string{seed.Reviewer1, seed.Reviewer2} {
		if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
			JobID: job.ID, Reviewer: r, Items: allPassInputs(ids),
		}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 0); err != nil {
		t.Fatalf("approve: %v", err)
	}
	rep, err := e.svc.Report.Build(job.ID, 1)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Approval == nil || rep.Approval.ApproverID != seed.UserPM {
		t.Fatalf("report must contain approval basis")
	}
	if !strings.Contains(rep.RenderMarkdown(), "签核依据") {
		t.Fatalf("markdown must contain approval basis section")
	}
}
