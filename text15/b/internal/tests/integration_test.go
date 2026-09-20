package tests

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"proofcycle/internal/model"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

var checklist3 = []string{"COLOR-01", "DIM-02", "BLEED-03"}

// TestFailedItemBlocksSignoff: a fail verdict (reason required) blocks PM
// sign-off with an explicit 422 and the blocking details.
func TestFailedItemBlocksSignoff(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1, r2 := a.mustUser(model.RoleReviewer), a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1, r2}, checklist3)

	if st, _ := a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes()); st != http.StatusCreated {
		t.Fatalf("upload status = %d", st)
	}

	// r1 passes everything; r2 fails COLOR-01 with a reason.
	if st, body := a.submitAll(r1.APIToken, j.id, 1, r1, model.VerdictPass, "", 0); st != http.StatusAccepted {
		t.Fatalf("r1 opinions status=%d body=%v", st, body)
	}
	failItems := []service.OpinionItemInput{
		{ChecklistCode: "COLOR-01", Verdict: model.VerdictFail, Reason: "Delta E 6 against target", ExpectedVersion: 0},
		{ChecklistCode: "DIM-02", Verdict: model.VerdictPass, ExpectedVersion: 0},
		{ChecklistCode: "BLEED-03", Verdict: model.VerdictNA, ExpectedVersion: 0},
	}
	st, body := a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", j.id), r2.APIToken,
		service.SubmitOpinionsInput{Version: 1, Items: failItems})
	if st != http.StatusAccepted {
		t.Fatalf("r2 opinions status=%d body=%v", st, body)
	}

	st, body = a.signoff(pm.APIToken, j.id)
	if st != http.StatusUnprocessableEntity {
		t.Fatalf("signoff status = %d, want 422; body=%v", st, body)
	}
	if body["code"] != "signoff_blocked" {
		t.Fatalf("code=%v want signoff_blocked", body["code"])
	}
	details, ok := body["details"].(map[string]any)
	if !ok {
		t.Fatalf("missing details: %v", body)
	}
	failed, _ := details["failed_by_reviewer"].(map[string]any)
	if len(failed) != 1 || !strings.Contains(fmt.Sprint(failed), "COLOR-01") {
		t.Fatalf("failed details wrong: %v", failed)
	}

	// After r2 changes the fail to pass, sign-off succeeds.
	fixItems := []service.OpinionItemInput{
		{ChecklistCode: "COLOR-01", Verdict: model.VerdictPass, ExpectedVersion: 1},
	}
	if st, body = a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", j.id), r2.APIToken,
		service.SubmitOpinionsInput{Version: 1, Items: fixItems}); st != http.StatusAccepted {
		t.Fatalf("fix opinion status=%d body=%v", st, body)
	}
	if st, body = a.signoff(pm.APIToken, j.id); st != http.StatusOK {
		t.Fatalf("signoff after fix status=%d body=%v", st, body)
	}
}

// TestFailRequiresReason: fail without a reason is rejected at submit time.
func TestFailRequiresReason(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)
	if st, _ := a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes()); st != 201 {
		t.Fatal("upload failed")
	}
	st, body := a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", j.id), r1.APIToken,
		service.SubmitOpinionsInput{Version: 1, Items: []service.OpinionItemInput{
			{ChecklistCode: "COLOR-01", Verdict: model.VerdictFail, ExpectedVersion: 0},
		}})
	if st != http.StatusUnprocessableEntity || body["code"] != "reason_required" {
		t.Fatalf("status=%d body=%v, want 422 reason_required", st, body)
	}
}

// TestPendingBlocksSignoff: untouched checklist items keep the round open.
func TestPendingBlocksSignoff(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1, r2 := a.mustUser(model.RoleReviewer), a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1, r2}, checklist3)
	a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes())

	// Only r1 answers; r2's items stay pending.
	if st, body := a.submitAll(r1.APIToken, j.id, 1, r1, model.VerdictPass, "", 0); st != 202 {
		t.Fatalf("r1 status=%d body=%v", st, body)
	}
	st, body := a.signoff(pm.APIToken, j.id)
	if st != 422 {
		t.Fatalf("signoff status=%d body=%v", st, body)
	}
	details := body["details"].(map[string]any)
	if len(details["pending_by_reviewer"].(map[string]any)) != 1 {
		t.Fatalf("pending detail: %v", details)
	}
}

// TestNewRevisionSupersedesOldRoundAndOpinions: v1 opinions are retained for
// history but can never be used to approve v2, and submitting against the
// superseded round is explicitly rejected.
func TestNewRevisionSupersedesOldRoundAndOpinions(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)

	a.upload(designer.APIToken, j.id, "v1.pdf", paddedPDF(200))
	if st, body := a.submitAll(r1.APIToken, j.id, 1, r1, model.VerdictPass, "", 0); st != 202 {
		t.Fatalf("v1 opinions %d %v", st, body)
	}

	// New revision supersedes the round without deleting opinions.
	if st, body := a.upload(designer.APIToken, j.id, "v2.pdf", paddedPDF(300)); st != 201 {
		t.Fatalf("v2 upload %d %v", st, body)
	}

	st, body := a.submitAll(r1.APIToken, j.id, 1, r1, model.VerdictPass, "", 0)
	if st != http.StatusUnprocessableEntity || body["code"] != "version_superseded" {
		t.Fatalf("superseded submit status=%d body=%v", st, body)
	}

	// PM cannot sign off v2: v1 opinions are complete but v2 has none.
	st, body = a.signoff(pm.APIToken, j.id)
	if st != 422 || body["code"] != "signoff_blocked" {
		t.Fatalf("signoff with stale opinions status=%d body=%v", st, body)
	}
	pending := body["details"].(map[string]any)["pending_by_reviewer"]
	if len(pending.(map[string]any)) != 1 {
		t.Fatalf("v2 should be entirely pending: %v", pending)
	}

	// History still exposes v1 opinions.
	st, v1 := a.do(http.MethodGet, fmt.Sprintf("/api/v1/jobs/%d/versions/1", j.id), r1.APIToken, nil)
	if st != 200 {
		t.Fatalf("v1 history %d", st)
	}
	if v1["status"] != model.RoundStatusSuperseded {
		t.Fatalf("v1 round status=%v", v1["status"])
	}
	cl := v1["checklist"].([]any)
	if len(cl) != 3 {
		t.Fatalf("checklist len=%d", len(cl))
	}
	ops := cl[0].(map[string]any)["opinions"].([]any)
	if len(ops) != 1 || ops[0].(map[string]any)["verdict"] != model.VerdictPass {
		t.Fatalf("v1 opinion not retained: %v", ops)
	}

	// Once v2 is fully reviewed, approval succeeds.
	if st, body = a.submitAll(r1.APIToken, j.id, 2, r1, model.VerdictPass, "", 0); st != 202 {
		t.Fatalf("v2 opinions %d %v", st, body)
	}
	if st, body = a.signoff(pm.APIToken, j.id); st != 200 {
		t.Fatalf("v2 signoff %d %v", st, body)
	}
	if got := a.jobDetail(pm.APIToken, j.id)["status"]; got != model.JobStatusApproved {
		t.Fatalf("job status=%v", got)
	}
}

// TestOpinionOptimisticConcurrency: a stale expected_version is rejected and
// the original opinion survives.
func TestOpinionOptimisticConcurrency(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)
	a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes())

	one := func(verdict, reason string, expect int) (int, map[string]any) {
		return a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", j.id), r1.APIToken,
			service.SubmitOpinionsInput{Version: 1, Items: []service.OpinionItemInput{
				{ChecklistCode: "COLOR-01", Verdict: verdict, Reason: reason, ExpectedVersion: expect},
			}})
	}
	if st, body := one(model.VerdictPass, "", 0); st != 202 {
		t.Fatalf("create %d %v", st, body)
	}
	if st, body := one(model.VerdictPass, "", 0); st != 409 {
		t.Fatalf("re-create expect 0 status=%d body=%v", st, body)
	}
	if st, body := one(model.VerdictNA, "", 5); st != 409 {
		t.Fatalf("stale expect status=%d body=%v", st, body)
	}
	if st, body := one(model.VerdictNA, "", 1); st != 202 {
		t.Fatalf("correct version update status=%d body=%v", st, body)
	}
	detail := a.jobDetail(r1.APIToken, j.id)
	round := findRound(detail, 1)
	op := round["checklist"].([]any)[0].(map[string]any)["opinions"].([]any)[0].(map[string]any)
	if op["verdict"] != model.VerdictNA || int(op["row_version"].(float64)) != 2 {
		t.Fatalf("unexpected opinion state: %v", op)
	}
}

// TestConcurrentSignoffOnlyOneApproves: parallel PM sign-offs must produce
// exactly one approval.
func TestConcurrentSignoffOnlyOneApproves(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1, r2 := a.mustUser(model.RoleReviewer), a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1, r2}, checklist3)
	a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes())
	a.submitAll(r1.APIToken, j.id, 1, r1, model.VerdictPass, "", 0)
	a.submitAll(r2.APIToken, j.id, 1, r2, model.VerdictPass, "", 0)

	const n = 8
	var wg sync.WaitGroup
	statuses := make([]int, n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			<-start
			st, _ := a.signoff(pm.APIToken, j.id)
			statuses[idx] = st
		}(i)
	}
	close(start)
	wg.Wait()

	ok, conflict := 0, 0
	for _, st := range statuses {
		switch st {
		case 200:
			ok++
		case 409:
			conflict++
		default:
			t.Fatalf("unexpected signoff status %d", st)
		}
	}
	if ok != 1 {
		t.Fatalf("exactly one signoff must succeed, got %d ok / %d conflict: %v", ok, conflict, statuses)
	}
	var signoffs int64
	a.db.Model(&model.Signoff{}).Where("job_id = ?", j.id).Count(&signoffs)
	if signoffs != 1 {
		t.Fatalf("signoff rows = %d, want 1", signoffs)
	}
}

// TestConcurrentUploadsProduceSequentialVersions: parallel revisions never
// collide on (job, version) and never lose a file.
func TestConcurrentUploadsProduceSequentialVersions(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)

	const n = 4
	var wg sync.WaitGroup
	errs := make(chan error, n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			_, _, err := a.svc.UploadRevision(context.Background(), designer, j.id,
				strings.NewReader(string(paddedPDF(120+i))))
			errs <- err
		}(i)
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent upload: %v", err)
		}
	}
	var versions []model.FileVersion
	if err := a.db.Where("job_id = ?", j.id).Order("version ASC").Find(&versions).Error; err != nil {
		t.Fatal(err)
	}
	if len(versions) != n {
		t.Fatalf("versions=%d want %d", len(versions), n)
	}
	for i, v := range versions {
		if v.Version != i+1 {
			t.Fatalf("version sequence gap: %+v", versions)
		}
		if _, err := os.Stat(v.StoredPath); err != nil {
			t.Fatalf("version %d file missing: %v", v.Version, err)
		}
	}
	var job model.Job
	a.db.First(&job, j.id)
	if job.CurrentVersion != n {
		t.Fatalf("current_version=%d want %d", job.CurrentVersion, n)
	}
}

// TestSignoffCannotRaceWithFailOpinion: a fail landing while PM signs must
// either precede the block or follow with a reopened-looking failure; the
// approved outcome may only occur when no fail exists.
func TestSignoffCannotRaceWithFailOpinion(t *testing.T) {
	for iter := 0; iter < 6; iter++ {
		a := newApp(t, 20)
		designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
		r1 := a.mustUser(model.RoleReviewer)
		j := a.mustJob(designer, pm, []*model.User{r1}, []string{"C1", "C2"})
		a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes())
		// Complete pass review first.
		a.submitAll(r1.APIToken, j.id, 1, r1, model.VerdictPass, "", 0)

		var wg sync.WaitGroup
		wg.Add(2)
		var signStatus int
		go func() {
			defer wg.Done()
			signStatus, _ = a.signoff(pm.APIToken, j.id)
		}()
		go func() {
			defer wg.Done()
			// Flip C1 to fail with reason, expected_version 1.
			_, _ = a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", j.id), r1.APIToken,
				service.SubmitOpinionsInput{Version: 1, Items: []service.OpinionItemInput{
					{ChecklistCode: "C1", Verdict: model.VerdictFail, Reason: "late defect", ExpectedVersion: 1},
				}})
		}()
		wg.Wait()

		var job model.Job
		a.db.First(&job, j.id)
		var failCount int64
		a.db.Model(&model.Opinion{}).
			Joins("JOIN review_rounds rr ON rr.id = opinions.round_id").
			Where("rr.job_id = ? AND rr.version = 1 AND opinions.verdict = ?", j.id, model.VerdictFail).
			Count(&failCount)
		if job.Status == model.JobStatusApproved && failCount > 0 {
			t.Fatalf("iteration %d: approved while a fail opinion exists", iter)
		}
		if signStatus == 200 && failCount > 0 {
			t.Fatalf("iteration %d: sign-off returned ok despite fail", iter)
		}
	}
}

// TestUploadInterruptedLeavesNoArtifacts: a context-cancelled mid-upload
// removes its temp file and creates no visible version. Startup recovery then
// finds nothing to repair.
func TestUploadInterruptedLeavesNoArtifacts(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)

	ctx, cancel := context.WithCancel(context.Background())
	pr, pw := io.Pipe()
	done := make(chan error, 1)
	go func() {
		_, _, err := a.svc.UploadRevision(ctx, designer, j.id, pr)
		done <- err
	}()
	chunk := make([]byte, 4096)
	for i := range chunk {
		chunk[i] = 'A'
	}
	if _, err := pw.Write(append([]byte("%PDF-1.4"), chunk...)); err != nil {
		t.Fatal(err)
	}
	cancel()
	_ = pw.Close()
	err := <-done
	if err == nil {
		t.Fatal("expected interrupted upload to fail")
	}

	var count int64
	a.db.Model(&model.FileVersion{}).Where("job_id = ?", j.id).Count(&count)
	if count != 0 {
		t.Fatalf("file_versions rows after interrupt = %d", count)
	}
	entries, _ := os.ReadDir(filepath.Join(a.rootDir, "storage", "tmp"))
	if len(entries) != 0 {
		t.Fatalf("temp files left behind: %d", len(entries))
	}
	if err := a.svc.RecoverOrphans(context.Background()); err != nil {
		t.Fatalf("recovery: %v", err)
	}
}

// TestRecoverGarbageCollectsOrphanFiles: a committed file whose database
// transaction was lost (rename landed, rows did not) is deleted at recovery.
func TestRecoverGarbageCollectsOrphanFiles(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)

	orphan := filepath.Join(a.rootDir, "storage", "files",
		fmt.Sprintf("job_%d", j.id), "v1", "orphan.pdf")
	if err := os.MkdirAll(filepath.Dir(orphan), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(orphan, pdfBytes(), 0o640); err != nil {
		t.Fatal(err)
	}
	// Leftover temp uploads are purged too.
	if err := os.WriteFile(filepath.Join(a.rootDir, "storage", "tmp", "leftover.part"),
		[]byte("%PDF-1.4 partial"), 0o640); err != nil {
		t.Fatal(err)
	}

	if err := a.svc.RecoverOrphans(context.Background()); err != nil {
		t.Fatalf("recover: %v", err)
	}
	if _, err := os.Stat(orphan); !os.IsNotExist(err) {
		t.Fatalf("orphan file not collected: %v", err)
	}
	if entries, _ := os.ReadDir(filepath.Join(a.rootDir, "storage", "tmp")); len(entries) != 0 {
		t.Fatalf("temp not purged: %v", entries)
	}

	// Version 1 is still free after cleanup.
	if _, _, err := a.svc.UploadRevision(context.Background(), designer, j.id,
		strings.NewReader(string(pdfBytes()))); err != nil {
		t.Fatalf("upload after recovery: %v", err)
	}
	var job model.Job
	a.db.First(&job, j.id)
	if job.CurrentVersion != 1 {
		t.Fatalf("current_version=%d", job.CurrentVersion)
	}
}

// TestRecoverKeepsCommittedFiles: recovery must never delete reachable files.
func TestRecoverKeepsCommittedFiles(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)
	a.upload(designer.APIToken, j.id, "v1.pdf", paddedPDF(123))
	a.upload(designer.APIToken, j.id, "v2.pdf", paddedPDF(456))

	var before []model.FileVersion
	a.db.Where("job_id = ?", j.id).Order("version").Find(&before)
	if err := a.svc.RecoverOrphans(context.Background()); err != nil {
		t.Fatalf("recover: %v", err)
	}
	for _, v := range before {
		if _, err := os.Stat(v.StoredPath); err != nil {
			t.Fatalf("committed file removed by recovery: %v", err)
		}
	}
}

// TestPathTraversalRejected: crafted paths never escape the storage root, and
// uploaded filenames cannot traverse directories.
func TestPathTraversalRejected(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)

	evil := filepath.Join(a.rootDir, "storage", "files", "..", "..", "escaped.pdf")
	if f, err := a.store.Open(evil); err == nil {
		f.Close()
		t.Fatal("Open accepted an escaping path")
	}
	if err := a.store.Remove(filepath.Join(a.rootDir, "..", "x.pdf")); err == nil {
		t.Fatal("Remove accepted escaping path")
	}

	// Upload whose multipart filename contains traversal sequences: the file
	// must still land in the server-generated layout, never outside it.
	st, body := a.upload(designer.APIToken, j.id, "../../../../tmp/pwn.pdf", pdfBytes())
	if st != 201 {
		t.Fatalf("upload status=%d body=%v", st, body)
	}
	var fv model.FileVersion
	if err := a.db.First(&fv, int64(body["version_id"].(float64))).Error; err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(fv.StoredPath, filepath.Join(a.rootDir, "storage")+string(os.PathSeparator)) {
		t.Fatalf("stored path escaped: %s", fv.StoredPath)
	}
	if _, err := os.Stat(filepath.Join(a.rootDir, "tmp", "pwn.pdf")); err == nil {
		t.Fatal("traversal filename wrote outside storage")
	}
	if strings.Contains(fv.StoredPath, "..") {
		t.Fatalf("stored path contains traversal: %s", fv.StoredPath)
	}
}

// TestUploadValidation: size cap, file-header sniffing and SHA-256 metadata.
func TestUploadValidation(t *testing.T) {
	a := newApp(t, 1) // 1 MiB cap
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)

	// Oversize.
	big := paddedPDF(2*1024*1024 + 10)
	st, body := a.upload(designer.APIToken, j.id, "big.pdf", big)
	if st != http.StatusRequestEntityTooLarge && st != http.StatusUnprocessableEntity {
		t.Fatalf("oversize status=%d body=%v", st, body)
	}

	// Wrong type (text) and wrong extension with PDF header etc.
	st, body = a.upload(designer.APIToken, j.id, "notes.txt", []byte("just plain text, not a pdf"))
	if st != http.StatusUnprocessableEntity || body["code"] != "unsupported_file_type" {
		t.Fatalf("text upload status=%d body=%v", st, body)
	}
	// GIF magic must be rejected.
	gif := append([]byte("GIF89a"), make([]byte, 32)...)
	st, body = a.upload(designer.APIToken, j.id, "x.png", gif)
	if st != http.StatusUnprocessableEntity {
		t.Fatalf("gif disguised as png status=%d body=%v", st, body)
	}
	// A PDF with .png extension is stored by detected type (extension ignored).
	st, body = a.upload(designer.APIToken, j.id, "lie.png", pdfBytes())
	if st != 201 || body["mime_type"] != storage.MimePDF {
		t.Fatalf("header-sniffed pdf status=%d body=%v", st, body)
	}
	// Real PNG works.
	st, body = a.upload(designer.APIToken, j.id, "sheet.png", pngBytes())
	if st != 201 || body["mime_type"] != storage.MimePNG {
		t.Fatalf("png status=%d body=%v", st, body)
	}
	if len(body["sha256"].(string)) != 64 {
		t.Fatalf("sha256 wrong: %v", body["sha256"])
	}
}

// TestTamperedFileRejected: if the stored file is corrupted on disk, download
// refuses to serve it (SHA-256 mismatch) and startup recovery reports the
// integrity failure rather than silently presenting altered content.
func TestTamperedFileRejected(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)
	st, body := a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes())
	if st != 201 {
		t.Fatalf("upload %v", body)
	}
	var fv model.FileVersion
	a.db.First(&fv, int64(body["version_id"].(float64)))
	if err := os.WriteFile(fv.StoredPath, []byte("%PDF-1.4 TAMPERED CONTENTS HERE"), 0o640); err != nil {
		t.Fatal(err)
	}

	st, got, _ := a.download(designer.APIToken, j.id, 1)
	if st != http.StatusInternalServerError {
		t.Fatalf("tampered download status=%d body=%s", st, got)
	}
	if err := a.svc.RecoverOrphans(context.Background()); err == nil {
		t.Fatal("recovery must report the corrupted file, got nil error")
	}
}

// TestRoleIsolation: reviewers cannot upload or sign off; designers cannot
// review their own job or approve it; outsiders see nothing.
func TestRoleIsolation(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1, outsider := a.mustUser(model.RoleReviewer), a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)
	a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes())

	// Reviewer cannot upload.
	if st, _ := a.upload(r1.APIToken, j.id, "x.pdf", pdfBytes()); st != http.StatusForbidden {
		t.Fatalf("reviewer upload status=%d want 403", st)
	}
	// Reviewer cannot sign off.
	if st, _ := a.signoff(r1.APIToken, j.id); st != http.StatusForbidden {
		t.Fatalf("reviewer signoff status=%d want 403", st)
	}
	// Designer cannot sign off (even though they are a participant).
	if st, _ := a.signoff(designer.APIToken, j.id); st != http.StatusForbidden {
		t.Fatalf("designer signoff status=%d want 403", st)
	}
	// Reviewer cannot submit opinions for a job they are not assigned to.
	j2 := a.mustJob(a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM),
		[]*model.User{a.mustUser(model.RoleReviewer)}, checklist3)
	a.upload(j2.designer.APIToken, j2.id, "v1.pdf", pdfBytes())
	st, body := a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", j2.id), r1.APIToken,
		service.SubmitOpinionsInput{Version: 1, Items: []service.OpinionItemInput{
			{ChecklistCode: checklist3[0], Verdict: model.VerdictPass, ExpectedVersion: 0},
		}})
	if st != http.StatusForbidden {
		t.Fatalf("unassigned reviewer submit status=%d body=%v", st, body)
	}

	// Outsider cannot read job, history, report or download the file.
	for _, path := range []string{
		fmt.Sprintf("/api/v1/jobs/%d", j.id),
		fmt.Sprintf("/api/v1/jobs/%d/versions/1", j.id),
		fmt.Sprintf("/api/v1/jobs/%d/versions/1/report", j.id),
	} {
		st, _ = a.do(http.MethodGet, path, outsider.APIToken, nil)
		if st != http.StatusForbidden {
			t.Fatalf("outsider GET %s status=%d want 403", path, st)
		}
	}
	st, _, _ = a.download(outsider.APIToken, j.id, 1)
	if st != http.StatusForbidden {
		t.Fatalf("outsider download status=%d want 403", st)
	}

	// Missing token and bad token are 401.
	if st, _ = a.do(http.MethodGet, fmt.Sprintf("/api/v1/jobs/%d", j.id), "", nil); st != http.StatusUnauthorized {
		t.Fatalf("no token status=%d want 401", st)
	}
	if st, _ = a.do(http.MethodGet, fmt.Sprintf("/api/v1/jobs/%d", j.id), "nope", nil); st != http.StatusUnauthorized {
		t.Fatalf("bad token status=%d want 401", st)
	}

	// List visibility: each participant sees the job; the outsider sees none.
	for _, tok := range []string{designer.APIToken, pm.APIToken, r1.APIToken} {
		st, body := a.do(http.MethodGet, "/api/v1/jobs", tok, nil)
		if st != 200 {
			t.Fatalf("list jobs as participant %d", st)
		}
		if !containsJobID(body["jobs"].([]any), j.id) {
			t.Fatalf("participant does not see their job %d", j.id)
		}
	}
	st, body = a.do(http.MethodGet, "/api/v1/jobs", outsider.APIToken, nil)
	if st != 200 || containsJobID(body["jobs"].([]any), j.id) || len(body["jobs"].([]any)) != 0 {
		t.Fatalf("outsider job list leaked: %v", body)
	}

	// A designer may not create a job whose PM is themselves.
	st, body = a.do(http.MethodPost, "/api/v1/jobs", designer.APIToken, service.CreateJobInput{
		Name: "self", PMID: designer.ID, ReviewerIDs: []int64{r1.ID},
		Checklist: []service.ChecklistInput{{Code: "C1", Description: "x"}},
	})
	if st != http.StatusBadRequest {
		t.Fatalf("self-PM job status=%d body=%v", st, body)
	}
}

// TestRevisionFilesAreImmutable: re-uploading never overwrites v1 bytes;
// download returns each version's original content.
func TestRevisionFilesAreImmutable(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)

	v1 := paddedPDF(150)
	v2 := paddedPDF(250)
	a.upload(designer.APIToken, j.id, "v1.pdf", v1)
	a.upload(designer.APIToken, j.id, "v2.pdf", v2)

	st, got1, _ := a.download(designer.APIToken, j.id, 1)
	if st != 200 || string(got1) != string(v1) {
		t.Fatalf("v1 bytes changed, st=%d len=%d want %d", st, len(got1), len(v1))
	}
	st, got2, hdr := a.download(designer.APIToken, j.id, 2)
	if st != 200 || string(got2) != string(v2) {
		t.Fatalf("v2 bytes mismatch, st=%d len=%d", st, len(got2))
	}
	if len(hdr.Get("X-SHA-256")) != 64 {
		t.Fatal("download missing SHA-256 header")
	}
}

// TestReportAuthorizationAndContent: report contains file summary, checklist
// and sign-off basis; unauthorized users are rejected.
func TestReportAuthorizationAndContent(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	outsider := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, checklist3)
	a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes())
	a.submitAll(r1.APIToken, j.id, 1, r1, model.VerdictPass, "", 0)
	a.signoff(pm.APIToken, j.id)

	st, body := a.do(http.MethodGet, fmt.Sprintf("/api/v1/jobs/%d/versions/1/report", j.id), pm.APIToken, nil)
	if st != 200 {
		t.Fatalf("json report %d %v", st, body)
	}
	round := body["round"].(map[string]any)
	fileSummary := round["file"].(map[string]any)
	if fileSummary["sha256"] == nil || len(round["checklist"].([]any)) != 3 {
		t.Fatalf("report content incomplete: %v", body)
	}

	// Markdown export includes the basis text.
	req, _ := http.NewRequest(http.MethodGet,
		fmt.Sprintf("%s/api/v1/jobs/%d/versions/1/report?format=markdown", a.server.URL, j.id), nil)
	req.Header.Set("X-API-Token", pm.APIToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	md, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 || !strings.Contains(string(md), "Sign-off basis") ||
		!strings.Contains(string(md), "SHA-256") {
		t.Fatalf("markdown report wrong: %d %s", resp.StatusCode, md)
	}

	st, _ = a.do(http.MethodGet, fmt.Sprintf("/api/v1/jobs/%d/versions/1/report", j.id), outsider.APIToken, nil)
	if st != 403 {
		t.Fatalf("outsider report status=%d want 403", st)
	}
}

// TestReviewerLimitAndUnknownVersion: at most 8 reviewers; opinions against a
// nonexistent version are 404.
func TestReviewerLimits(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	var nine []int64
	for i := 0; i < 9; i++ {
		nine = append(nine, a.mustUser(model.RoleReviewer).ID)
	}
	st, body := a.do(http.MethodPost, "/api/v1/jobs", designer.APIToken, service.CreateJobInput{
		Name: "toomany", PMID: pm.ID, ReviewerIDs: nine,
		Checklist: []service.ChecklistInput{{Code: "C1", Description: "x"}},
	})
	if st != 400 {
		t.Fatalf("9 reviewers status=%d body=%v", st, body)
	}

	one := []int64{nine[0]}
	st, body = a.do(http.MethodPost, "/api/v1/jobs", designer.APIToken, service.CreateJobInput{
		Name: "oneok", PMID: pm.ID, ReviewerIDs: one,
		Checklist: []service.ChecklistInput{{Code: "C1", Description: "x"}},
	})
	if st != 201 {
		t.Fatalf("1 reviewer status=%d body=%v", st, body)
	}
	jobID := int64(body["id"].(float64))
	st, body = a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", jobID), a.dbUserToken(nine[0]),
		service.SubmitOpinionsInput{Version: 99, Items: []service.OpinionItemInput{
			{ChecklistCode: "C1", Verdict: model.VerdictPass, ExpectedVersion: 0},
		}})
	if st != 404 {
		t.Fatalf("unknown version status=%d body=%v", st, body)
	}
}

// dbUserToken fetches the token for a seeded user id.
func (a *app) dbUserToken(id int64) string {
	var u model.User
	if err := a.db.First(&u, id).Error; err != nil {
		a.t.Fatal(err)
	}
	return u.APIToken
}

func containsJobID(jobs []any, id int64) bool {
	for _, j := range jobs {
		if int64(j.(map[string]any)["id"].(float64)) == id {
			return true
		}
	}
	return false
}

// TestInvalidVerdictRejected: only pass/fail/na are allowed.
func TestInvalidVerdictRejected(t *testing.T) {
	a := newApp(t, 20)
	designer, pm := a.mustUser(model.RoleDesigner), a.mustUser(model.RolePM)
	r1 := a.mustUser(model.RoleReviewer)
	j := a.mustJob(designer, pm, []*model.User{r1}, []string{"C1"})
	a.upload(designer.APIToken, j.id, "v1.pdf", pdfBytes())
	st, body := a.do(http.MethodPost, fmt.Sprintf("/api/v1/jobs/%d/opinions", j.id), r1.APIToken,
		map[string]any{"version": 1, "items": []map[string]any{
			{"checklist_code": "C1", "verdict": "approved", "expected_version": 0},
		}})
	if st != 400 {
		t.Fatalf("invalid verdict status=%d body=%v", st, body)
	}
}
