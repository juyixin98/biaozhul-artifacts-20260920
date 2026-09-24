package registry

import (
	"bytes"
	"context"
	"testing"
	"time"

	"layerregistry/internal/gc"
)

// TestRecoveryRestoresWhenRowSurvived simulates the "quarantined, tx rolled
// back" crash window: the file is in quarantine while the blobs row still
// exists. Recovery must restore bytes to the CAS tree.
func TestRecoveryRestoresWhenRowSurvived(t *testing.T) {
	e := newTestEnv(t)
	content := []byte("restore-me-after-crash")
	dg := e.uploadBlob("alpha", content)

	runID := "crash-run-before-commit"
	// Persist an unfinished GC run as the real sweep would.
	if err := e.st.InsertGCRun(context.Background(), runID, 0); err != nil {
		t.Fatal(err)
	}
	if err := e.st.MarkGCSweeping(context.Background(), runID, 123, time.Now(), 1, 0); err != nil {
		t.Fatal(err)
	}
	// Move the file to quarantine WITHOUT deleting the row (the crash window).
	if err := e.fs.Quarantine(runID, dg); err != nil {
		t.Fatal(err)
	}
	if e.blobRowCount(dg) != 1 {
		t.Fatal("precondition: row should survive")
	}
	if e.fs.BlobExists(dg) {
		t.Fatal("precondition: file should be quarantined")
	}

	col := gc.New(e.st, e.fs, gc.Config{})
	logLines, err := col.Recover(context.Background())
	if err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if len(logLines) < 2 {
		t.Fatalf("expected recovery log lines, got %v", logLines)
	}
	if !e.fs.BlobExists(dg) {
		t.Fatal("file must be restored to CAS after recovery")
	}
	got, err := e.fs.VerifyBlobFile(dg)
	if err != nil {
		t.Fatalf("restored file fails hash verification: %v", err)
	}
	if got != int64(len(content)) {
		t.Fatalf("restored size=%d want=%d", got, len(content))
	}
	resp, b := e.do("GET", "/v2/alpha/blobs/"+dg, nil, nil)
	if resp.StatusCode != 200 || !bytes.Equal(b, content) {
		t.Fatalf("blob not readable intact after recovery: %d", resp.StatusCode)
	}
	rec, err := e.st.GetGCRun(context.Background(), runID)
	if err != nil || rec.State != "recovered" {
		t.Fatalf("run state=%v err=%v, want recovered", rec, err)
	}
}

// TestRecoveryPurgesWhenRowDeleted simulates the "tx committed, bytes not yet
// purged" crash window: the row is gone while the file sits in quarantine.
// Recovery must purge the file.
func TestRecoveryPurgesWhenRowDeleted(t *testing.T) {
	e := newTestEnv(t)
	content := []byte("purge-me-after-crash")
	dg := e.uploadBlob("alpha", content)

	runID := "crash-run-after-commit"
	if err := e.st.InsertGCRun(context.Background(), runID, 0); err != nil {
		t.Fatal(err)
	}
	if err := e.st.MarkGCSweeping(context.Background(), runID, 123, time.Now(), 1, 0); err != nil {
		t.Fatal(err)
	}
	if err := e.fs.Quarantine(runID, dg); err != nil {
		t.Fatal(err)
	}
	// Delete the row durably (the committed sweep delete).
	tx, err := e.st.Pool().Begin(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if err := e.st.DeleteBlobRow(context.Background(), tx, dg); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(context.Background()); err != nil {
		t.Fatal(err)
	}
	if e.blobRowCount(dg) != 0 {
		t.Fatal("precondition: row should be deleted")
	}

	col := gc.New(e.st, e.fs, gc.Config{})
	if _, err := col.Recover(context.Background()); err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if e.fs.BlobExists(dg) {
		t.Fatal("quarantined file must be purged, but it is back in CAS")
	}
	qd, _ := e.fs.ListQuarantine()
	if len(qd[runID]) != 0 {
		t.Fatalf("quarantine not emptied: %v", qd[runID])
	}
	resp, _ := e.do("GET", "/v2/alpha/blobs/"+dg, nil, nil)
	if resp.StatusCode != 404 {
		t.Fatalf("deleted blob should be 404 after recovery, got %d", resp.StatusCode)
	}
}
