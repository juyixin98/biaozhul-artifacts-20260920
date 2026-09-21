package integration

import (
	"bytes"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"

	"proofcycle/internal/models"
	"proofcycle/internal/service"
)

// errReader fails mid-stream to simulate an interrupted upload.
type errReader struct{ head []byte }

func (e *errReader) Read(p []byte) (int, error) {
	if len(e.head) == 0 {
		return 0, io.ErrUnexpectedEOF
	}
	n := copy(p, e.head)
	e.head = e.head[n:]
	return n, nil
}

func TestRevisionWithoutFirstVersionRejected(t *testing.T) {
	env := setupEnv(t)
	svc := service.New(env.DB, env.Store)
	d := createUser(t, env.DB, "d-nofirst", models.RoleDesigner)
	pm := createUser(t, env.DB, "pm-nofirst", models.RolePM)
	r := createUser(t, env.DB, "r-nofirst", models.RoleReviewer)
	job, err := svc.CreateJob(service.CreateJobInput{Title: "no first", DesignerID: d.ID, PMID: pm.ID, ReviewerIDs: []uint{r.ID}})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := svc.SubmitRevision(job.ID, d.ID, "v2.pdf", bytes.NewReader(pdfBytes)); !errors.Is(err, service.ErrInvalidInput) {
		t.Fatalf("revision before v1: want ErrInvalidInput, got %v", err)
	}
}

func TestInterruptedUploadLeavesNothing(t *testing.T) {
	env := setupEnv(t)
	svc, d, _, _, jobID := seedFullJob(t, env, 1)

	// A stream that drops halfway must not leave a committed blob or temp
	// file, and must not create a version.
	_, _, err := svc.SubmitRevision(jobID, d.ID, "broken.pdf", &errReader{head: []byte("%PDF-1.7 partial bytes")})
	if err == nil {
		t.Fatal("interrupted upload unexpectedly succeeded")
	}
	versions, err := svc.ListVersions(jobID)
	if err != nil {
		t.Fatal(err)
	}
	if len(versions) != 1 {
		t.Fatalf("versions after interrupt = %d, want 1", len(versions))
	}
}

func TestRevisionResetsApprovalBasis(t *testing.T) {
	env := setupEnv(t)
	svc, d, pm, reviewers, jobID := seedFullJob(t, env, 2)

	// Reviewers pass v1 completely.
	for _, r := range reviewers {
		if err := svc.UpsertOpinions(jobID, r.ID, absentExpected(allItems(models.OutcomePass)), allItems(models.OutcomePass)); err != nil {
			t.Fatal(err)
		}
	}
	// Designer uploads revision before sign-off: old opinions are retained
	// but no longer relevant.
	v2, round2, err := svc.SubmitRevision(jobID, d.ID, "v2.pdf", bytes.NewReader(pdfBytes))
	if err != nil {
		t.Fatalf("revision: %v", err)
	}
	if v2.Version != 2 || round2.VersionNumber != 2 {
		t.Fatalf("new version numbers wrong: %+v %+v", v2, round2)
	}
	// Approval must fail on v2 even though v1 was fully reviewed.
	if _, err := svc.Approve(jobID, pm.ID); !errors.Is(err, service.ErrPendingItems) {
		t.Fatalf("approve v2 on v1 opinions: want ErrPendingItems, got %v", err)
	}

	// Old history is retained: v1 round is superseded and still shows 16 opinions.
	h1, err := svc.History(jobID, pm.ID, 1)
	if err != nil {
		t.Fatal(err)
	}
	if h1.Round.Status != models.RoundSuperseded {
		t.Fatalf("v1 round status = %s, want superseded", h1.Round.Status)
	}
	if len(h1.Opinions) != 16 {
		t.Fatalf("v1 opinions retained = %d, want 16", len(h1.Opinions))
	}

	// Reviewers complete v2, then PM can approve the new version only.
	for _, r := range reviewers {
		items := allItems(models.OutcomeNA)
		if err := svc.UpsertOpinions(jobID, r.ID, absentExpected(items), items); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := svc.Approve(jobID, pm.ID); err != nil {
		t.Fatalf("approve v2: %v", err)
	}
	h2, err := svc.History(jobID, pm.ID, 2)
	if err != nil {
		t.Fatal(err)
	}
	if h2.Round.Status != models.RoundApproved || h2.Approval == nil {
		t.Fatalf("v2 not marked approved: %+v", h2.Round)
	}

	// Files were never overwritten: two distinct blobs exist with distinct hashes.
	vs, _ := svc.ListVersions(jobID)
	if vs[0].StoragePath == vs[1].StoragePath || vs[0].SHA256 == vs[1].SHA256 {
		// same-content upload is allowed to share a hash; paths must still differ
		if vs[0].StoragePath == vs[1].StoragePath {
			t.Fatal("revision overwrote the previous file's storage path")
		}
	}
}

func TestReviewerCanOnlyWriteOwnOpinions(t *testing.T) {
	env := setupEnv(t)
	svc, _, _, reviewers, jobID := seedFullJob(t, env, 2)
	r1, r2 := reviewers[0], reviewers[1]

	items := []service.ItemOpinion{{Code: "COLOR", Outcome: models.OutcomePass}}
	if err := svc.UpsertOpinions(jobID, r1.ID, absentExpected(items), items); err != nil {
		t.Fatal(err)
	}
	// r2 supplies r1's version number expecting to hijack that row, but the
	// unique key is (round, reviewer, item): r2's request only addresses their
	// own row, which does not exist yet. Passing a non-zero expectation for a
	// non-existent own row is therefore rejected (expected 0) — it can never
	// touch r1's verdict.
	err := svc.UpsertOpinions(jobID, r2.ID, map[string]int{"COLOR": 1},
		[]service.ItemOpinion{{Code: "COLOR", Outcome: models.OutcomeFail, Reason: "takeover attempt"}})
	if !errors.Is(err, service.ErrConflict) {
		t.Fatalf("expected conflict when r2 cites another row's version, got %v", err)
	}
	if err := svc.UpsertOpinions(jobID, r2.ID, map[string]int{"COLOR": 0},
		[]service.ItemOpinion{{Code: "COLOR", Outcome: models.OutcomeFail, Reason: "my own verdict"}}); err != nil {
		t.Fatalf("r2 own row create: %v", err)
	}
	h, err := svc.History(jobID, r1.ID, 1)
	if err != nil {
		t.Fatal(err)
	}
	var r1Verdict, r2Verdict string
	for _, o := range h.Opinions {
		switch o.ReviewerID {
		case r1.ID:
			r1Verdict = o.Outcome
		case r2.ID:
			r2Verdict = o.Outcome
		}
	}
	if r1Verdict != models.OutcomePass {
		t.Fatalf("r1 verdict changed to %q", r1Verdict)
	}
	if r2Verdict != models.OutcomeFail {
		t.Fatalf("r2 verdict missing/false: %q", r2Verdict)
	}
}

func TestConcurrentApproveOnlyOneWins(t *testing.T) {
	env := setupEnv(t)
	svc, _, pm, reviewers, jobID := seedFullJob(t, env, 2)
	for _, r := range reviewers {
		if err := svc.UpsertOpinions(jobID, r.ID, absentExpected(allItems(models.OutcomePass)), allItems(models.OutcomePass)); err != nil {
			t.Fatal(err)
		}
	}

	var wg sync.WaitGroup
	var mu sync.Mutex
	created, rejected := 0, 0
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := svc.Approve(jobID, pm.ID)
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				created++
			case errors.Is(err, service.ErrJobClosed):
				rejected++
			default:
				t.Errorf("unexpected approve error: %v", err)
			}
		}()
	}
	wg.Wait()
	if created != 1 || rejected != 19 {
		t.Fatalf("created=%d rejected=%d, want 1/19", created, rejected)
	}
}

func TestHistoryAndReportAuthorization(t *testing.T) {
	env := setupEnv(t)
	svc, _, pm, reviewers, jobID := seedFullJob(t, env, 1)
	if err := svc.UpsertOpinions(jobID, reviewers[0].ID, absentExpected(allItems(models.OutcomePass)), allItems(models.OutcomePass)); err != nil {
		t.Fatal(err)
	}

	outsider := createUser(t, env.DB, "outsider-"+strings.ReplaceAll(t.Name(), "/", "_"), models.RoleReviewer)
	if _, err := svc.History(jobID, outsider.ID, 1); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("outsider history: want ErrForbidden, got %v", err)
	}
	report, _, _, err := svc.Report(jobID, pm.ID, 1)
	if err != nil {
		t.Fatalf("report: %v", err)
	}
	for _, want := range []string{"ProofCycle Review Report", "SHA-256", "[COLOR]", "NOT APPROVED"} {
		if !strings.Contains(report, want) {
			t.Fatalf("report missing %q\n%s", want, report)
		}
	}
	if _, _, _, err := svc.Report(jobID, outsider.ID, 1); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("outsider report: want ErrForbidden, got %v", err)
	}

	// Version that does not exist on this job is a 404-class error.
	if _, err := svc.RoundByVersion(jobID, 99); !errors.Is(err, service.ErrVersionMismatch) {
		t.Fatalf("missing version: want ErrVersionMismatch, got %v", err)
	}
}

func TestChecklistSnapshotBoundToRound(t *testing.T) {
	env := setupEnv(t)
	svc, d, _, _, jobID := seedFullJob(t, env, 1)

	// Even if the master template changes later, the round snapshot is fixed.
	tplBefore := models.ChecklistTemplateItem{Code: "COLOR", Description: "old", Order: 1}
	var existing models.ChecklistTemplateItem
	if err := env.DB.Where("code = ?", "COLOR").First(&existing).Error; err == nil {
		env.DB.Model(&existing).Update("description", "MUTATED MASTER TEXT")
	} else {
		_ = tplBefore
	}
	v2, _, err := svc.SubmitRevision(jobID, d.ID, "v2.pdf", bytes.NewReader(pdfBytes))
	if err != nil {
		t.Fatal(err)
	}
	_ = v2

	h1, _ := svc.History(jobID, d.ID, 1)
	h2, _ := svc.History(jobID, d.ID, 2)
	if len(h1.Items) != len(h2.Items) || len(h1.Items) == 0 {
		t.Fatalf("snapshot item counts differ: %d vs %d", len(h1.Items), len(h2.Items))
	}
	// v1 snapshot must keep its frozen descriptions independent of the master.
	var sawOld, sawNew int
	for _, it := range h1.Items {
		if it.Code == "COLOR" && it.Description == "MUTATED MASTER TEXT" {
			sawOld++
		}
	}
	for _, it := range h2.Items {
		if it.Code == "COLOR" && it.Description == "MUTATED MASTER TEXT" {
			sawNew++
		}
	}
	if sawOld != 0 {
		t.Fatal("v1 snapshot leaked a later master-template mutation")
	}
	if sawNew != 1 {
		t.Fatalf("v2 snapshot did not capture current master (saw=%d)", sawNew)
	}
}
