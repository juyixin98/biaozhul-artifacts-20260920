package registry_test

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Scenario 1: two images sharing a layer.  Untagging one image must keep the
// shared layer; only the image-private layer becomes garbage.  Every decision
// must carry an auditable reason.
func TestSharedLayerIsRetained(t *testing.T) {
	e := newEnv(t)

	dShared := e.pushBlob("images", []byte("shared-base-layer-data"))
	dOnlyA := e.pushBlob("images", []byte("layer-only-in-image-A"))
	dOnlyB := e.pushBlob("images", []byte("layer-only-in-image-B"))
	cdA := e.pushBlob("images", []byte("{\"a\":1}"))
	cdB := e.pushBlob("images", []byte("{\"b\":2}"))

	mA := e.putManifest("images", "imgA", cdA, []string{dShared, dOnlyA})
	e.putManifest("images", "imgB", cdB, []string{dShared, dOnlyB})

	for _, d := range []string{dShared, dOnlyA, dOnlyB} {
		if !e.exists(d) {
			t.Fatalf("layer %s missing before GC", d)
		}
	}

	// Delete image A entirely (untag then delete manifest): dOnlyA and A's
	// config lose their only roots; dShared must survive because image B
	// still references it.
	e.deleteTag("images", "imgA")
	e.deleteManifest(mA)

	rep := e.runGC("")
	// Deleted: dOnlyA + cdA (2).  Retained late: none.  Marked reachable:
	// dShared, dOnlyB, cdB (3).
	if got := num(rep, "deleted_count"); got != 2 {
		t.Fatalf("expected 2 deletions (A-private layer + A config), got %v: %s", got, pretty(rep))
	}
	if got := num(rep, "retained_count"); got != 0 {
		t.Fatalf("expected 0 late-retained candidates, got %v", got)
	}
	if got := num(rep, "marked_count"); got != 3 {
		t.Fatalf("expected 3 marked-reachable blobs (shared+onlyB+cdB), got %v", got)
	}

	if e.exists(dOnlyA) {
		t.Fatal("A-private layer should have been deleted")
	}
	if e.exists(cdA) {
		t.Fatal("A config should have been deleted once its manifest is gone")
	}
	if !e.exists(dShared) {
		t.Fatal("shared layer wrongly deleted while imgB still references it")
	}
	if !e.exists(dOnlyB) {
		t.Fatal("B-private layer wrongly deleted while imgB is tagged")
	}
	if !e.exists(cdB) {
		t.Fatal("B config wrongly deleted while imgB is tagged")
	}

	decisions := map[string]map[string]any{}
	for _, it := range rep["items"].([]any) {
		m := it.(map[string]any)
		decisions[m["blob_digest"].(string)] = m
	}
	if d, ok := decisions[dShared]; !ok || d["decision"] != "retain" ||
		!strings.Contains(d["reason"].(string), "reachable") {
		t.Fatalf("shared layer audit row wrong: %+v", d)
	}
	if d, ok := decisions[dOnlyA]; !ok || d["decision"] != "delete" ||
		!strings.Contains(d["reason"].(string), "unreferenced") {
		t.Fatalf("A-private layer audit row wrong: %+v", d)
	}

	// The persisted report endpoint returns the same evidence afterwards.
	fetched := e.gcReport(int64(num(rep, "run_id")))
	if num(fetched, "deleted_count") != 2 {
		t.Fatalf("persisted GC report inconsistent: %s", pretty(fetched))
	}
}

// Scenario 2a (in-process, deterministic): an active read lease pins a
// candidate layer during sweep; releasing it lets the next GC delete it.
func TestPullDeleteRace_Lease(t *testing.T) {
	e := newEnv(t)

	d := e.pushBlob("img", randomBytes(4096)) // never tagged -> candidate

	ids, err := e.reg.AcquireLease(e.ctx, "test-puller", 30*time.Second, []string{d})
	if err != nil {
		t.Fatalf("lease: %v", err)
	}

	rep := e.runGC("")
	if num(rep, "deleted_count") != 0 || num(rep, "retained_count") != 1 {
		t.Fatalf("leased layer must be retained, report: %s", pretty(rep))
	}
	if !e.exists(d) {
		t.Fatal("leased layer deleted during active pull")
	}
	for _, it := range rep["items"].([]any) {
		m := it.(map[string]any)
		if m["blob_digest"] == d && !strings.Contains(m["reason"].(string), "read lease") {
			t.Fatalf("retain reason must cite active read lease: %v", m["reason"])
		}
	}

	if err := e.reg.ReleaseLease(e.ctx, ids); err != nil {
		t.Fatalf("release: %v", err)
	}
	rep2 := e.runGC("")
	if num(rep2, "deleted_count") != 1 {
		t.Fatalf("after lease release layer should be deleted: %s", pretty(rep2))
	}
	if e.exists(d) {
		t.Fatal("layer not deleted after lease release")
	}
}

// Scenario 2b (real HTTP): a GET that is mid-stream holds its lease for the
// whole transfer; GC running concurrently retains the layer.
func TestPullDeleteRace_HTTP(t *testing.T) {
	e := newEnv(t)

	// 16 MiB exceeds the socket write buffer, so the server stays blocked in
	// io.Copy (holding the lease) while we hold the pull open.
	d := e.pushBlob("img", bytes.Repeat([]byte("Z"), 16<<20))

	req, _ := http.NewRequest(http.MethodGet, e.ts.URL+"/v2/img/blobs/"+d, nil)
	resp, err := e.ts.Client().Transport.RoundTrip(req)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("get status %d", resp.StatusCode)
	}
	if resp.Header.Get("X-Lease-Ids") == "" {
		t.Fatal("expected X-Lease-Ids proving lease acquisition before streaming")
	}
	one := make([]byte, 1)
	if _, err := io.ReadFull(resp.Body, one); err != nil {
		t.Fatalf("read first byte: %v", err)
	}
	waitForLease(t, e, d)

	rep := e.runGC("")
	if num(rep, "deleted_count") != 0 {
		t.Fatalf("mid-stream blob deleted: %s", pretty(rep))
	}
	if !e.exists(d) {
		t.Fatal("mid-stream blob deleted under an active HTTP pull")
	}
	_, _ = io.Copy(io.Discard, resp.Body) // finish pull -> lease released
}

// Scenario 2c (real HTTP, pause hook): GC is paused after mark; while paused
// a real pull acquires a lease on the candidate; the resumed sweep must see it.
func TestPullArrivesDuringMarkSweepPause(t *testing.T) {
	e := newEnv(t)
	// 16 MiB exceeds the socket write buffer, so the server stays blocked in
	// io.Copy (holding the lease) while only one byte is read.
	layer := bytes.Repeat([]byte("Q"), 16<<20)
	d := e.pushBlob("img", layer)

	token := "race-lease-token"
	t.Cleanup(func() { resumeIfPaused(e, token) })
	done := make(chan map[string]any, 1)
	errCh := make(chan error, 1)
	go func() {
		rep, err := gcPOST(e, "?pause_after_mark=1&pause_token="+url.QueryEscape(token))
		if err != nil {
			errCh <- err
			return
		}
		done <- rep
	}()

	waitForGate(t, e, token)

	// Real pull starts and stays open.
	req, _ := http.NewRequest(http.MethodGet, e.ts.URL+"/v2/img/blobs/"+d, nil)
	resp, err := e.ts.Client().Transport.RoundTrip(req)
	if err != nil {
		t.Fatalf("get during pause: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("get status %d", resp.StatusCode)
	}
	one := make([]byte, 1)
	if n, rerr := io.ReadFull(resp.Body, one); rerr != nil || n != 1 {
		t.Fatalf("pull read first byte: n=%d err=%v", n, rerr)
	}
	waitForLease(t, e, d) // block until the pull's lease row is durably visible

	resumeGC(t, e, token)
	select {
	case rep := <-done:
		if num(rep, "deleted_count") != 0 || num(rep, "retained_count") != 1 {
			t.Fatalf("layer pulled during scanning must survive: %s", pretty(rep))
		}
		if !e.exists(d) {
			t.Fatal("layer deleted despite lease acquired during mark/sweep gap")
		}
	case err := <-errCh:
		t.Fatalf("gc: %v", err)
	case <-time.After(10 * time.Second):
		t.Fatal("GC did not finish after resume")
	}
	_, _ = io.Copy(io.Discard, resp.Body)
}

// Scenario 3: a NEW MANIFEST referencing the candidate layer lands after the
// mark snapshot but before sweep.  Sweep-time re-verification under
// SERIALIZABLE + FOR UPDATE must retain the freshly referenced layer — the
// core "don't delete layers referenced during scanning" guarantee.
func TestNewTagAfterMarkIsRechecked(t *testing.T) {
	e := newEnv(t)

	d := e.pushBlob("images", []byte("layer-that-gets-a-new-tag"))
	cd := e.pushBlob("images", []byte("{}"))
	// Layer is uploaded but NOT yet referenced by any stored manifest: it is
	// a candidate at mark time.
	manifestRaw := e.buildManifest(cd, []string{d})
	md := digestOf(manifestRaw)

	token := "new-tag-token"
	t.Cleanup(func() { resumeIfPaused(e, token) })
	done := make(chan map[string]any, 1)
	errCh := make(chan error, 1)
	go func() {
		rep, err := gcPOST(e, "?pause_after_mark=1&pause_token="+url.QueryEscape(token))
		if err != nil {
			errCh <- err
			return
		}
		done <- rep
	}()

	waitForGate(t, e, token)

	// The manifest (and thus the reference to d) lands AFTER the snapshot,
	// and is tagged in the same request.
	gotDigest := e.putRawManifest("images", "late-tag", manifestRaw)
	if gotDigest != md {
		t.Fatalf("manifest digest mismatch: %s vs %s", gotDigest, md)
	}

	resumeGC(t, e, token)
	select {
	case rep := <-done:
		// Both the layer and its config were unreferenced at the snapshot,
		// and both became reachable when the manifest landed mid-GC.
		if num(rep, "candidate_count") != 2 {
			t.Fatalf("expected 2 candidates at snapshot, got %v: %s",
				num(rep, "candidate_count"), pretty(rep))
		}
		if num(rep, "deleted_count") != 0 {
			t.Fatalf("newly referenced blobs must not be deleted: %s", pretty(rep))
		}
		if num(rep, "retained_count") != 2 {
			t.Fatalf("sweep must retain the newly referenced layer+config: %s", pretty(rep))
		}
		foundLayer, foundConfig := false, false
		for _, it := range rep["items"].([]any) {
			m := it.(map[string]any)
			switch m["blob_digest"] {
			case d:
				foundLayer = true
				if !strings.Contains(m["reason"].(string), "newly referenced") {
					t.Fatalf("reason should explain post-snapshot reference: %v", m["reason"])
				}
			case cd:
				foundConfig = true
			}
		}
		if !foundLayer {
			t.Fatal("no audit item for the newly referenced layer")
		}
		if !foundConfig {
			t.Fatal("no audit item for the newly referenced config")
		}
	case err := <-errCh:
		t.Fatalf("gc: %v", err)
	case <-time.After(10 * time.Second):
		t.Fatal("GC did not finish after resume")
	}
	if !e.exists(d) {
		t.Fatal("layer referenced during scanning was deleted")
	}
	if !e.exists(cd) {
		t.Fatal("config referenced during scanning was deleted")
	}

	// Once the tag and then the manifest are removed, a later GC collects the
	// now-unreferenced layer and config.
	e.deleteTag("images", "late-tag")
	e.deleteManifest(md)
	rep2 := e.runGC("")
	if num(rep2, "deleted_count") != 2 {
		t.Fatalf("layer+config should be collectable after manifest removal: %s", pretty(rep2))
	}
	if e.exists(d) {
		t.Fatal("layer still present after manifest deletion + GC")
	}
}

// Scenario 4: a GC run left mid-flight by a crashed process is detected and
// marked crashed; the next run completes safely and deletes nothing twice.
func TestGCCrashRecovery(t *testing.T) {
	e := newEnv(t)

	dKeep := e.pushBlob("images", []byte("tagged-keep"))
	dGarb := e.pushBlob("images", []byte("untagged-garbage"))
	cd := e.pushBlob("images", []byte("{}"))
	e.putManifest("images", "alive", cd, []string{dKeep})

	// Seed a run stuck mid-sweep, as if the process died after mark committed.
	// At that point: 2 marked (the kept layer + its config), 1 candidate.
	var staleID int64
	if err := e.db.QueryRowContext(e.ctx,
		`INSERT INTO gc_runs(status,marked_count,candidate_count)
		 VALUES ('sweeping',$1,$2) RETURNING id`, 2, 1).Scan(&staleID); err != nil {
		t.Fatalf("seed stale run: %v", err)
	}

	rep, err := e.gc.Run(e.ctx)
	if err != nil {
		t.Fatalf("recovered gc run: %v", err)
	}
	if !rep.RecoveredCrash {
		t.Fatal("report must flag recovery of a previous crashed run")
	}
	if rep.Deleted != 1 || rep.Retained != 0 {
		t.Fatalf("post-crash GC should delete 1 garbage blob, got deleted=%d retained=%d",
			rep.Deleted, rep.Retained)
	}
	if !e.exists(dKeep) {
		t.Fatal("tagged layer deleted after crash recovery")
	}
	if e.exists(dGarb) {
		t.Fatal("garbage layer not collected after crash recovery")
	}

	var state string
	if err := e.db.QueryRowContext(e.ctx,
		`SELECT status FROM gc_runs WHERE id=$1`, staleID).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != "crashed" {
		t.Fatalf("stale run state = %s, want crashed", state)
	}
	if rep.Status != "completed" {
		t.Fatalf("new run status = %s, want completed", rep.Status)
	}

	// Idempotency: no double delete, clean no-op.
	rep2, err := e.gc.Run(e.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if rep2.Deleted != 0 || rep2.Candidates != 0 {
		t.Fatalf("second run should be a no-op, got deleted=%d candidates=%d",
			rep2.Deleted, rep2.Candidates)
	}
}

// Scenario 5: uploads are staged to temp and only published after real digest
// verification.  A claimed digest that does not match content is rejected;
// the temp file and bookkeeping are removed.
func TestDigestVerificationRejectsBadContent(t *testing.T) {
	e := newEnv(t)

	// Snapshot the temp dir and blobs table around a bad commit.
	before, _ := os.ReadDir(e.tmpDir())
	status, body := e.pushBlobBadDigest("images", []byte("actual-body"), []byte("something-else"))
	if status != http.StatusBadRequest {
		t.Fatalf("expected 400 for digest mismatch, got %d: %s", status, body)
	}
	if !strings.Contains(body, "digest mismatch") {
		t.Fatalf("error should explain digest mismatch: %s", body)
	}
	bad := digestOf([]byte("something-else"))
	if e.exists(bad) {
		t.Fatal("blob with mismatching digest was published")
	}
	var n int
	if err := e.db.QueryRow(`SELECT count(*) FROM blob_uploads`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != len(before) {
		t.Fatalf("rejected upload left bookkeeping behind: %d rows", n)
	}
	// No newly-stranded temp file.
	after, _ := os.ReadDir(e.tmpDir())
	if len(after) != len(before) {
		t.Fatalf("rejected upload left a temp file behind: before=%d after=%d",
			len(before), len(after))
	}

	// The same content with the CORRECT digest publishes fine (real SHA-256).
	good := e.pushBlob("images", []byte("actual-body"))
	if good != digestOf([]byte("actual-body")) {
		t.Fatal("good digest not computed/published correctly")
	}
	if !e.exists(good) {
		t.Fatal("correctly verified blob missing from CAS")
	}
}

// Scenario 6: orphan temp files are cleaned by the dedicated maintenance path
// with distinct audit reasons, separate from layer-GC accounting.
func TestOrphanTempFilesCleaned(t *testing.T) {
	e := newEnv(t)

	// (a) temp file with no bookkeeping row.
	orphanPath := filepath.Join(e.tmpDir(), "abandoned-upload")
	if err := os.WriteFile(orphanPath, []byte("partial-upload-bytes"), 0o640); err != nil {
		t.Fatal(err)
	}
	// (b) completed row whose temp file vanished (crash between commit and
	// publish).  Cleaner should drop the dangling row too.
	gonePath := filepath.Join(e.tmpDir(), "gone-row")
	if _, err := e.db.ExecContext(e.ctx,
		`INSERT INTO blob_uploads(upload_id,temp_path,completed) VALUES ($1,$2,TRUE)`,
		"gone-row", gonePath); err != nil {
		t.Fatal(err)
	}

	rep, err := e.cleaner.CleanOrphans(e.ctx, false)
	if err != nil {
		t.Fatalf("cleanup: %v", err)
	}
	// Both the file-less orphan and the file-less bookkeeping row are removed.
	if rep.Removed != 1 {
		t.Fatalf("expected 1 removed orphan FILE (the dangling row has no bytes), got %d: %+v",
			rep.Removed, rep.Items)
	}
	if _, err := os.Stat(orphanPath); !os.IsNotExist(err) {
		t.Fatal("orphan temp file not removed")
	}
	var n int
	if err := e.db.QueryRow(`SELECT count(*) FROM blob_uploads WHERE upload_id='gone-row'`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatal("dangling completed upload row not cleaned")
	}
	// Both distinct audit reasons must be present.
	var reasons []string
	for _, it := range rep.Items {
		reasons = append(reasons, it.Reason)
	}
	if !containsReason(reasons, "no blob_uploads row") {
		t.Fatalf("missing orphan-file audit reason: %v", reasons)
	}
	if !containsReason(reasons, "temp file gone") {
		t.Fatalf("missing dangling-row audit reason: %v", reasons)
	}

	// Layer GC's own audit tables are untouched by orphan maintenance.
	var gcItems int
	if err := e.db.QueryRow(`SELECT count(*) FROM gc_items`).Scan(&gcItems); err != nil {
		t.Fatal(err)
	}
	if gcItems != 0 {
		t.Fatal("orphan maintenance must not write into gc_items")
	}
}

// A blob that is merely uploaded but never referenced is collected once, and
// its deletion is audited end-to-end through the HTTP report.
func TestUnreferencedBlobCollected(t *testing.T) {
	e := newEnv(t)
	d := e.pushBlob("img", []byte("lonely-layer"))
	rep := e.runGC("")
	if num(rep, "deleted_count") != 1 {
		t.Fatalf("expected deletion of unreferenced blob: %s", pretty(rep))
	}
	if e.exists(d) {
		t.Fatal("unreferenced blob not deleted")
	}
	// A re-run does not error on the now-missing blob.
	rep2 := e.runGC("")
	if num(rep2, "candidate_count") != 0 {
		t.Fatalf("second run should see no blobs: %s", pretty(rep2))
	}
}

// ---- HTTP pause/resume plumbing for deterministic races --------------------

func gcPOST(e *env, query string) (map[string]any, error) {
	resp, err := e.ts.Client().Post(e.ts.URL+"/admin/gc/run"+query, "application/json", nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("gc http %d: %s", resp.StatusCode, b)
	}
	var m map[string]any
	if err := decodeJSON(resp.Body, &m); err != nil {
		return nil, err
	}
	return m, nil
}

func waitForGate(t *testing.T, e *env, token string) {
	t.Helper()
	var gateCh chan struct{}
	e.srv.WaitGateMu(func(g map[string]chan struct{}) {
		gateCh = g[token]
	})
	deadline := time.Now().Add(5 * time.Second)
	for gateCh == nil && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
		e.srv.WaitGateMu(func(g map[string]chan struct{}) { gateCh = g[token] })
	}
	if gateCh == nil {
		t.Fatal("GC pause gate never opened")
	}
}

func resumeGC(t *testing.T, e *env, token string) {
	t.Helper()
	resp, err := e.ts.Client().Post(
		e.ts.URL+"/admin/gc/resume?token="+url.QueryEscape(token), "application/json", nil)
	if err != nil {
		t.Fatalf("resume: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("resume status %d", resp.StatusCode)
	}
}

// resumeIfPaused releases a still-open pause gate so a GC goroutine never
// outlives the test and blocks httptest server shutdown.
func resumeIfPaused(e *env, token string) {
	var gate chan struct{}
	e.srv.WaitGateMu(func(g map[string]chan struct{}) { gate = g[token] })
	if gate != nil {
		select {
		case <-gate: // already closed
		default:
			close(gate)
		}
		e.srv.WaitGateMu(func(g map[string]chan struct{}) { delete(g, token) })
	}
}

// waitForLease blocks until an active (unexpired) lease row exists for d.
func waitForLease(t *testing.T, e *env, d string) {
	t.Helper()
	waitUntil(t, 5*time.Second, func() bool {
		var n int
		if err := e.db.QueryRow(
			`SELECT count(*) FROM leases WHERE blob_digest=$1 AND expires_at > now()`,
			d).Scan(&n); err != nil {
			return false
		}
		return n > 0
	})
}
