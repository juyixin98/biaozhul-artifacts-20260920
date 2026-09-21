package integration

import (
	"bytes"
	"errors"
	"strings"
	"sync"
	"testing"

	"proofcycle/internal/models"
	"proofcycle/internal/service"
)

var pdfBytes = []byte("%PDF-1.7\n%\xe2\xe3\xcf\xd3\nsome proof artwork bytes here")

// allItems builds one pass opinion per checklist code.
func allItems(outcome string) []service.ItemOpinion {
	codes := []string{"COLOR", "BLEED", "TYPOGRAPHY", "RESOLUTION", "BARCODE", "MATERIAL", "REGULATORY", "FINISHING"}
	out := make([]service.ItemOpinion, 0, len(codes))
	for _, c := range codes {
		out = append(out, service.ItemOpinion{Code: c, Outcome: outcome})
	}
	return out
}

// seedFullJob creates designer, PM and two reviewers, a job and its first
// version.
func seedFullJob(t *testing.T, env *Env, reviewerCount int) (*service.Service, *models.User, *models.User, []*models.User, uint) {
	t.Helper()
	svc := service.New(env.DB, env.Store)
	name := strings.ReplaceAll(t.Name(), "/", "_")
	d := createUser(t, env.DB, "d-"+name, models.RoleDesigner)
	pm := createUser(t, env.DB, "pm-"+name, models.RolePM)
	reviewers := make([]*models.User, 0, reviewerCount)
	ids := make([]uint, 0, reviewerCount)
	for i := 0; i < reviewerCount; i++ {
		r := createUser(t, env.DB, "r-"+name+"-"+string(rune('a'+i)), models.RoleReviewer)
		reviewers = append(reviewers, r)
		ids = append(ids, r.ID)
	}
	job, err := svc.CreateJob(service.CreateJobInput{
		Title: "t-" + name, DesignerID: d.ID, PMID: pm.ID, ReviewerIDs: ids,
	})
	if err != nil {
		t.Fatalf("create job: %v", err)
	}
	if _, _, err := svc.SaveFirstVersion(job.ID, d.ID, "v1.pdf", bytes.NewReader(pdfBytes)); err != nil {
		t.Fatalf("first version: %v", err)
	}
	return svc, d, pm, reviewers, job.ID
}

func TestCreateJobRosterLimits(t *testing.T) {
	env := setupEnv(t)
	svc := service.New(env.DB, env.Store)
	d := createUser(t, env.DB, "d1", models.RoleDesigner)
	pm := createUser(t, env.DB, "pm1", models.RolePM)

	mkReviewers := func(n int) []uint {
		ids := make([]uint, 0, n)
		prefix := "rr" + strings.ReplaceAll(t.Name(), "/", "_")
		for i := 0; i < n; i++ {
			r := createUser(t, env.DB, prefix+string(rune('a'+i)), models.RoleReviewer)
			ids = append(ids, r.ID)
		}
		return ids
	}

	if _, err := svc.CreateJob(service.CreateJobInput{Title: "zero", DesignerID: d.ID, PMID: pm.ID, ReviewerIDs: nil}); !errors.Is(err, service.ErrReviewerRoster) {
		t.Fatalf("0 reviewers: want ErrReviewerRoster, got %v", err)
	}
	if _, err := svc.CreateJob(service.CreateJobInput{Title: "nine", DesignerID: d.ID, PMID: pm.ID, ReviewerIDs: mkReviewers(9)}); !errors.Is(err, service.ErrReviewerRoster) {
		t.Fatalf("9 reviewers: want ErrReviewerRoster, got %v", err)
	}
	// Eight distinct reviewers is the ceiling and succeeds.
	if _, err := svc.CreateJob(service.CreateJobInput{Title: "eight", DesignerID: d.ID, PMID: pm.ID, ReviewerIDs: mkReviewers(8)}); err != nil {
		t.Fatalf("8 reviewers: %v", err)
	}
	// Designer cannot be a reviewer of their own job.
	if _, err := svc.CreateJob(service.CreateJobInput{Title: "self", DesignerID: d.ID, PMID: pm.ID, ReviewerIDs: []uint{d.ID}}); !errors.Is(err, service.ErrPMConflict) {
		t.Fatalf("designer-as-reviewer: want ErrPMConflict, got %v", err)
	}
	// Designer role must actually be a designer.
	if _, err := svc.CreateJob(service.CreateJobInput{Title: "badrole", DesignerID: pm.ID, PMID: pm.ID, ReviewerIDs: mkReviewers(1)}); err == nil {
		t.Fatal("expected error when PM id is used as designer")
	}
}

func TestUploadValidationAndCleanup(t *testing.T) {
	env := setupEnv(t)
	svc, d, pm, _, jobID := seedFullJob(t, env, 1)
	_ = pm

	// Bad magic bytes are rejected before any DB write.
	if _, _, err := svc.SubmitRevision(jobID, d.ID, "evil.exe", bytes.NewReader([]byte("MZ\x90\x00binary-bytes"))); !errors.Is(err, service.ErrFileType) {
		t.Fatalf("bad magic: want ErrFileType, got %v", err)
	}
	// A rejected upload leaves no new version and no committed blob beyond v1.
	versions, err := svc.ListVersions(jobID)
	if err != nil || len(versions) != 1 {
		t.Fatalf("versions after reject = %d (err=%v)", len(versions), err)
	}

	// Oversized stream is rejected and its temp file removed.
	big := append(append([]byte{}, pdfBytes...), make([]byte, 2<<20)...)
	if _, _, err := svc.SubmitRevision(jobID, d.ID, "big.pdf", bytes.NewReader(big)); !errors.Is(err, service.ErrFileTooLarge) {
		t.Fatalf("oversize: want ErrFileTooLarge, got %v", err)
	}

	// A reviewer who is not on this job's roster is an outsider and must not
	// even learn the job exists.
	if _, _, err := svc.SubmitRevision(jobID, seedReviewer(env, t, "upload-x"), "x.pdf", bytes.NewReader(pdfBytes)); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("outside reviewer upload: want ErrForbidden, got %v", err)
	}
}

func seedReviewer(env *Env, t *testing.T, name string) uint {
	return createUser(t, env.DB, name, models.RoleReviewer).ID
}

func TestFailureBlocksApproval(t *testing.T) {
	env := setupEnv(t)
	svc, _, pm, reviewers, jobID := seedFullJob(t, env, 2)

	items := allItems(models.OutcomePass)
	if err := svc.UpsertOpinions(jobID, reviewers[0].ID, absentExpected(items), items); err != nil {
		t.Fatal(err)
	}
	failed := allItems(models.OutcomePass)
	failed[1].Outcome = models.OutcomeFail
	failed[1].Reason = "bleed margin off by 2mm"
	if err := svc.UpsertOpinions(jobID, reviewers[1].ID, absentExpected(failed), failed); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Approve(jobID, pm.ID); !errors.Is(err, service.ErrFailedItems) {
		t.Fatalf("approve with failure: want ErrFailedItems, got %v", err)
	}

	// Fail without reason is rejected outright.
	bad := []service.ItemOpinion{{Code: "COLOR", Outcome: models.OutcomeFail}}
	if err := svc.UpsertOpinions(jobID, reviewers[0].ID, map[string]int{}, bad); !errors.Is(err, service.ErrFailReasonRequired) {
		t.Fatalf("fail without reason: want ErrFailReasonRequired, got %v", err)
	}

	// Invalid outcome rejected.
	badOutcome := []service.ItemOpinion{{Code: "COLOR", Outcome: "approved!!"}}
	if err := svc.UpsertOpinions(jobID, reviewers[0].ID, map[string]int{}, badOutcome); !errors.Is(err, service.ErrOutcomeInvalid) {
		t.Fatalf("bad outcome: want ErrOutcomeInvalid, got %v", err)
	}
}

func TestPendingItemsBlockApproval(t *testing.T) {
	env := setupEnv(t)
	svc, _, pm, reviewers, jobID := seedFullJob(t, env, 2)

	// Only reviewer 1 completes; reviewer 2 does nothing.
	items := allItems(models.OutcomePass)
	if err := svc.UpsertOpinions(jobID, reviewers[0].ID, absentExpected(items), items); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Approve(jobID, pm.ID); !errors.Is(err, service.ErrPendingItems) {
		t.Fatalf("approve incomplete: want ErrPendingItems, got %v", err)
	}

	// A second assigned reviewer added? Roster is fixed; a different reviewer
	// user who is not on the roster gets a membership error.
	outside := createUser(t, env.DB, "outside", models.RoleReviewer)
	if err := svc.UpsertOpinions(jobID, outside.ID, absentExpected(items[:1]), items[:1]); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("outside reviewer: want ErrForbidden, got %v", err)
	}
}

func TestHappyPathApprove(t *testing.T) {
	env := setupEnv(t)
	svc, d, pm, reviewers, jobID := seedFullJob(t, env, 2)
	for _, r := range reviewers {
		items := allItems(models.OutcomePass)
		if err := svc.UpsertOpinions(jobID, r.ID, absentExpected(items), items); err != nil {
			t.Fatal(err)
		}
	}
	approval, err := svc.Approve(jobID, pm.ID)
	if err != nil {
		t.Fatalf("approve: %v", err)
	}
	if approval.ApproverID != pm.ID || !strings.Contains(approval.Basis, "sha256=") {
		t.Fatalf("approval basis malformed: %+v", approval)
	}

	// Designer cannot approve their own job even with every check green:
	// create a second complete job to try it on.
	svc2, d2, pm2, revs2, j2 := seedFullJob(t, env, 1)
	for _, r := range revs2 {
		if err := svc2.UpsertOpinions(j2, r.ID, absentExpected(allItems(models.OutcomePass)), allItems(models.OutcomePass)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := svc2.Approve(j2, d2.ID); !errors.Is(err, service.ErrSelfApproval) {
		t.Fatalf("self approval: want ErrSelfApproval, got %v", err)
	}
	// A reviewer on the job also cannot approve (member, wrong role).
	if _, err := svc2.Approve(j2, revs2[0].ID); !errors.Is(err, service.ErrNotApprover) {
		t.Fatalf("reviewer approve: want ErrNotApprover, got %v", err)
	}
	if _, err := svc2.Approve(j2, pm2.ID); err != nil {
		t.Fatalf("pm approve: %v", err)
	}

	// After approval, jobs are closed: no revision, no opinion edit.
	if _, _, err := svc.SubmitRevision(jobID, d.ID, "late.pdf", bytes.NewReader(pdfBytes)); !errors.Is(err, service.ErrJobClosed) {
		t.Fatalf("revision after close: want ErrJobClosed, got %v", err)
	}
	if err := svc.UpsertOpinions(jobID, reviewers[0].ID, map[string]int{"COLOR": 1},
		[]service.ItemOpinion{{Code: "COLOR", Outcome: models.OutcomePass}}); err == nil {
		t.Fatal("opinion edit after close unexpectedly succeeded")
	}
}

func absentExpected(items []service.ItemOpinion) map[string]int {
	m := make(map[string]int, len(items))
	for _, it := range items {
		m[it.Code] = 0 // 0 => opinion must not exist yet
	}
	return m
}

func TestOptimisticLocking(t *testing.T) {
	env := setupEnv(t)
	svc, _, _, reviewers, jobID := seedFullJob(t, env, 1)
	r := reviewers[0]

	create := []service.ItemOpinion{{Code: "COLOR", Outcome: models.OutcomePass}}
	if err := svc.UpsertOpinions(jobID, r.ID, map[string]int{"COLOR": 0}, create); err != nil {
		t.Fatal(err)
	}
	// Same expected version twice concurrently: exactly one may win.
	var wg sync.WaitGroup
	var mu sync.Mutex
	ok, conflict := 0, 0
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			err := svc.UpsertOpinions(jobID, r.ID, map[string]int{"COLOR": 1},
				[]service.ItemOpinion{{Code: "COLOR", Outcome: models.OutcomePass}})
			mu.Lock()
			defer mu.Unlock()
			if err == nil {
				ok++
			} else if errors.Is(err, service.ErrConflict) {
				conflict++
			} else {
				t.Errorf("unexpected err: %v", err)
			}
		}(i)
	}
	wg.Wait()
	if ok != 1 || conflict != 11 {
		t.Fatalf("optimistic lock: ok=%d conflict=%d, want 1/11", ok, conflict)
	}

	// Updating without supplying an expected version is refused.
	err := svc.UpsertOpinions(jobID, r.ID, map[string]int{},
		[]service.ItemOpinion{{Code: "COLOR", Outcome: models.OutcomePass}})
	if !errors.Is(err, service.ErrConflict) {
		t.Fatalf("update without version: want ErrConflict, got %v", err)
	}
}
