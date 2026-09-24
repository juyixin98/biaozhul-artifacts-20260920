package registry

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"layerregistry/internal/storage"
	"layerregistry/internal/store"
)

// testEnv bundles a real PostgreSQL-backed registry for integration tests.
type testEnv struct {
	t       *testing.T
	st      *store.Store
	fs      *storage.Store
	srv     *Server
	http    *httptest.Server
	dataDir string
}

const testDBURL = "postgres://registry:registry_pw@127.0.0.1:5432/registry_test?sslmode=disable"

func truncateAll(t *testing.T, st *store.Store) {
	t.Helper()
	_, err := st.Pool().Exec(context.Background(), `TRUNCATE
		gc_audit_events, gc_items, gc_runs, leases, uploads,
		tags, manifest_refs, manifests, blobs CASCADE`)
	if err != nil {
		t.Fatalf("truncate: %v", err)
	}
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	if os.Getenv("RUN_PG_TESTS") == "" {
		t.Skip("set RUN_PG_TESTS=1 to run PostgreSQL integration tests")
	}
	ctx := context.Background()
	st, err := store.New(ctx, testDBURL)
	if err != nil {
		t.Skipf("PostgreSQL test DB unavailable (%v); start it to run integration tests", err)
	}
	t.Cleanup(st.Close)
	truncateAll(t, st)

	dir := t.TempDir()
	fs, err := storage.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	srv := NewServer(st, fs, Config{
		LeaseTTL:      30 * time.Second,
		BlobGrace:     0,
		OrphanMaxAge:  time.Hour,
		FaultsEnabled: true,
	})
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)
	return &testEnv{t: t, st: st, fs: fs, srv: srv, http: hs, dataDir: dir}
}

// --- HTTP helpers ---------------------------------------------------------

func (e *testEnv) do(method, path string, hdr http.Header, body []byte) (*http.Response, []byte) {
	e.t.Helper()
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, e.http.URL+path, rdr)
	if err != nil {
		e.t.Fatal(err)
	}
	for k, vs := range hdr {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp, b
}

func sha256hex(b []byte) string {
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// uploadBlob performs a single-shot monolithic upload; returns digest.
func (e *testEnv) uploadBlob(repo string, content []byte) string {
	e.t.Helper()
	dg := sha256hex(content)
	h := http.Header{}
	resp, b := e.do("POST", "/v2/"+repo+"/blobs/uploads/?digest="+dg, h, content)
	if resp.StatusCode != http.StatusCreated {
		e.t.Fatalf("upload blob %s: status %d body %s", dg[:16], resp.StatusCode, b)
	}
	return dg
}

// imageManifestBody builds a minimal valid OCI image manifest JSON.
func imageManifestBody(config string, layers ...string) []byte {
	ls := []string{}
	for _, l := range layers {
		ls = append(ls, fmt.Sprintf(`{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":%q,"size":1}`, l))
	}
	body := fmt.Sprintf(`{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",`+
		`"config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":%q,"size":1},`+
		`"layers":[%s]}`, config, strings.Join(ls, ","))
	return []byte(body)
}

func (e *testEnv) putManifest(repo, ref string, body []byte) string {
	e.t.Helper()
	h := http.Header{"Content-Type": {"application/vnd.oci.image.manifest.v1+json"}}
	resp, b := e.do("PUT", "/v2/"+repo+"/manifests/"+ref, h, body)
	if resp.StatusCode != http.StatusCreated {
		e.t.Fatalf("PUT manifest %s/%s: %d %s", repo, ref, resp.StatusCode, b)
	}
	return resp.Header.Get("Docker-Content-Digest")
}

type gcItem struct {
	Kind, Repo, Digest, Decision, Phase, Reason string
}

type gcReport struct {
	RunID            string   `json:"run_id"`
	State            string   `json:"state"`
	DeletedBlobs     int      `json:"deleted_blobs"`
	RetainedBlobs    int      `json:"retained_blobs"`
	DeletedManifests int      `json:"deleted_manifests"`
	Items            []gcItem `json:"items"`
}

func (e *testEnv) runGC(body string) gcReport {
	e.t.Helper()
	resp, b := e.do("POST", "/admin/gc", http.Header{"Content-Type": {"application/json"}}, []byte(body))
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("run GC: %d %s", resp.StatusCode, b)
	}
	var rep gcReport
	if err := json.Unmarshal(b, &rep); err != nil {
		e.t.Fatalf("decode GC report: %v body=%s", err, b)
	}
	return rep
}

func (e *testEnv) itemDecision(rep gcReport, digest string) (gcItem, bool) {
	for _, it := range rep.Items {
		if it.Digest == digest {
			return it, true
		}
	}
	return gcItem{}, false
}

func (e *testEnv) blobRowCount(digest string) int {
	var n int
	if err := e.st.Pool().QueryRow(context.Background(),
		"SELECT count(*) FROM blobs WHERE digest=$1", digest).Scan(&n); err != nil {
		e.t.Fatal(err)
	}
	return n
}

// --- Tests ----------------------------------------------------------------

// TestSharedLayerRetention: two images share one layer; deleting one image's
// tag must not GC the shared layer.
func TestSharedLayerRetention(t *testing.T) {
	e := newTestEnv(t)

	shared := []byte("shared-base-layer")
	onlyA := []byte("alpha-private-layer")
	onlyB := []byte("beta-private-layer")
	cfgA := e.uploadBlob("alpha", []byte(`{"c":"a"}`))
	cfgB := e.uploadBlob("beta", []byte(`{"c":"b"}`))
	LD := e.uploadBlob("alpha", shared)
	AD := e.uploadBlob("alpha", onlyA)
	BD := e.uploadBlob("beta", onlyB)

	e.putManifest("alpha", "v1", imageManifestBody(cfgA, LD, AD))
	e.putManifest("beta", "v1", imageManifestBody(cfgB, LD, BD))

	// Baseline GC: everything reachable.
	rep := e.runGC("{}")
	if rep.DeletedBlobs != 0 || rep.RetainedBlobs != 5 {
		t.Fatalf("baseline: deleted=%d retained=%d, want 0/5", rep.DeletedBlobs, rep.RetainedBlobs)
	}
	it, ok := e.itemDecision(rep, LD)
	if !ok || it.Decision != "retain" || !strings.Contains(it.Reason, "alpha") || !strings.Contains(it.Reason, "beta") {
		t.Fatalf("shared layer retain reason should cite both images: %+v", it)
	}

	// Delete beta; its unique layer/config become garbage, L stays.
	// Tag updates create short manifest read leases by design; clear them so
	// the delete is not blocked (a lease expiry would do the same).
	if _, err := e.st.Pool().Exec(context.Background(), "DELETE FROM leases"); err != nil {
		t.Fatal(err)
	}
	resp, b := e.do("DELETE", "/v2/beta/manifests/v1", nil, nil)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("delete beta: %d %s", resp.StatusCode, b)
	}
	rep = e.runGC("{}")
	if rep.DeletedBlobs != 2 {
		t.Fatalf("after delete beta: deleted blobs=%d want 2", rep.DeletedBlobs)
	}
	if it, ok := e.itemDecision(rep, LD); !ok || it.Decision != "retain" {
		t.Fatalf("shared layer must be retained: %+v", it)
	}
	if it, ok := e.itemDecision(rep, AD); !ok || it.Decision != "retain" {
		t.Fatalf("alpha private layer must be retained: %+v", it)
	}
	if it, ok := e.itemDecision(rep, BD); !ok || it.Decision != "delete" {
		t.Fatalf("beta private layer must be deleted: %+v", it)
	}
	if e.blobRowCount(LD) != 1 {
		t.Fatal("shared layer row disappeared")
	}
	if e.fs.BlobExists(LD) != true {
		t.Fatal("shared layer file disappeared")
	}
	// The shared blob is still fully readable.
	resp, got := e.do("GET", "/v2/alpha/blobs/"+LD, nil, nil)
	if resp.StatusCode != http.StatusOK || !bytes.Equal(got, shared) {
		t.Fatalf("shared layer pull after GC: status=%d match=%v", resp.StatusCode, bytes.Equal(got, shared))
	}
}

// TestPullDeleteRace: a pull in flight must keep the blob even though GC runs
// concurrently; once the pull ends (lease released), GC collects it.
func TestPullDeleteRace(t *testing.T) {
	e := newTestEnv(t)
	content := bytes.Repeat([]byte("raced-"), 20000)
	dg := e.uploadBlob("alpha", content)

	var wg sync.WaitGroup
	wg.Add(1)
	pullErr := error(nil)
	pullStatus := 0
	var pulled []byte
	go func() {
		defer wg.Done()
		req, _ := http.NewRequest("GET", e.http.URL+"/v2/alpha/blobs/"+dg, nil)
		req.Header.Set("X-Read-Delay", "1500")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			pullErr = err
			return
		}
		defer resp.Body.Close()
		pullStatus = resp.StatusCode
		pulled, _ = io.ReadAll(resp.Body)
	}()

	time.Sleep(400 * time.Millisecond)
	rep := e.runGC("{}") // while pull is active
	it, ok := e.itemDecision(rep, dg)
	if !ok {
		t.Fatalf("blob missing from GC report")
	}
	if it.Decision != "retain" || !strings.Contains(it.Reason, "lease") {
		t.Fatalf("blob pulled must be retained by lease, got %+v", it)
	}
	wg.Wait()
	if pullErr != nil || pullStatus != http.StatusOK || !bytes.Equal(pulled, content) {
		t.Fatalf("pull failed: err=%v status=%d bytesMatch=%v", pullErr, pullStatus, bytes.Equal(pulled, content))
	}

	// Lease released on pull end; GC must now delete it.
	rep = e.runGC("{}")
	it, ok = e.itemDecision(rep, dg)
	if !ok || it.Decision != "delete" {
		t.Fatalf("blob should be collected after pull, got %+v", it)
	}
	if e.blobRowCount(dg) != 0 || e.fs.BlobExists(dg) {
		t.Fatal("blob should be gone from DB and disk")
	}
}

// TestTagAfterMark: a tag published in the mark/sweep gap must save a layer
// that the mark snapshot considered garbage.
func TestTagAfterMark(t *testing.T) {
	e := newTestEnv(t)
	content := []byte("late-referenced-layer")
	cfg := e.uploadBlob("alpha", []byte(`{"c":"late"}`))
	dg := e.uploadBlob("alpha", content)
	body := imageManifestBody(cfg, dg)
	md := sha256hex(body)

	// Drive GC in a goroutine with a mark/sweep pause; publish inside the gap.
	var wg sync.WaitGroup
	wg.Add(1)
	var rep gcReport
	go func() {
		defer wg.Done()
		rep = e.runGC(`{"mark_sweep_delay":1200}`)
	}()
	time.Sleep(400 * time.Millisecond)
	got := e.putManifest("alpha", "late", body)
	if got != md {
		t.Fatalf("late manifest digest %s != %s", got, md)
	}
	wg.Wait()

	it, ok := e.itemDecision(rep, dg)
	if !ok || it.Decision != "retain" {
		t.Fatalf("layer referenced during scan must NOT be deleted: %+v", it)
	}
	if !strings.Contains(it.Reason, "retained-after-mark") {
		t.Fatalf("retain should be a sweep-time recheck: %+v", it)
	}
	// Tag and blob are intact afterwards.
	if e.blobRowCount(dg) != 1 {
		t.Fatal("late-tagged layer row missing after GC")
	}
	resp, b := e.do("GET", "/v2/alpha/manifests/late", nil, nil)
	if resp.StatusCode != http.StatusOK || sha256hex(b) != md {
		t.Fatalf("late tag fetch: %d digestMatch=%v", resp.StatusCode, sha256hex(b) == md)
	}
}

// TestChunkedUploadAndDigestMismatch: resumable chunks assemble correctly and
// a wrong claimed digest is rejected with a real hash comparison.
func TestChunkedUploadAndDigestMismatch(t *testing.T) {
	e := newTestEnv(t)

	// Initiate.
	resp, _ := e.do("POST", "/v2/alpha/blobs/uploads/", nil, nil)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("initiate: %d", resp.StatusCode)
	}
	loc := resp.Header.Get("Location")
	if loc == "" {
		t.Fatal("missing Location")
	}
	id := resp.Header.Get("Docker-Upload-UUID")

	// Patch two chunks.
	h := http.Header{"Content-Range": {"bytes 0-3/*"}}
	resp, _ = e.do("PATCH", loc, h, []byte("aaaa"))
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("patch1: %d", resp.StatusCode)
	}
	h = http.Header{"Content-Range": {"bytes 4-7/*"}}
	resp, _ = e.do("PATCH", loc, h, []byte("bbbb"))
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("patch2: %d", resp.StatusCode)
	}
	// Wrong offset rejected.
	h = http.Header{"Content-Range": {"bytes 0-1/*"}}
	resp, _ = e.do("PATCH", loc, h, []byte("zz"))
	if resp.StatusCode != http.StatusRequestedRangeNotSatisfiable {
		t.Fatalf("out-of-order patch: status=%d want 416", resp.StatusCode)
	}

	// Finalize with the true digest.
	want := sha256hex([]byte("aaaabbbb"))
	resp, _ = e.do("PUT", loc+"?digest="+want, nil, nil)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("finalize: %d", resp.StatusCode)
	}
	if !e.fs.BlobExists(want) {
		t.Fatal("chunked blob not published")
	}

	// Wrong digest on a single-shot upload is rejected.
	resp, b := e.do("POST",
		"/v2/alpha/blobs/uploads/?digest="+sha256hex([]byte("nope")), nil, []byte("real bytes"))
	if resp.StatusCode != http.StatusBadRequest || !strings.Contains(string(b), "DIGEST_MISMATCH") {
		t.Fatalf("mismatched upload: status=%d body=%s", resp.StatusCode, b)
	}
	_ = id
}

// TestOrphanTempCleanup: expired sessions and unlinked temp files are handled
// separately and audited; a recent live session is preserved.
func TestOrphanTempCleanup(t *testing.T) {
	e := newTestEnv(t)
	// Live session.
	resp, _ := e.do("POST", "/v2/alpha/blobs/uploads/", nil, nil)
	liveID := resp.Header.Get("Docker-Upload-UUID")
	// Expired session (backdate its row).
	resp, _ = e.do("POST", "/v2/alpha/blobs/uploads/", nil, nil)
	oldID := resp.Header.Get("Docker-Upload-UUID")
	if _, err := e.st.Pool().Exec(context.Background(),
		"UPDATE uploads SET started_at=now()-interval '3 hours' WHERE id=$1", oldID); err != nil {
		t.Fatal(err)
	}
	// Unlinked junk file.
	junk := e.fs.TempPath("junk-" + strings.Repeat("f", 8))
	if err := os.WriteFile(junk, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	resp, b := e.do("POST", "/admin/orphans", nil, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("orphans: %d %s", resp.StatusCode, b)
	}
	var out struct {
		ExpiredSessions   []string `json:"expired_sessions"`
		UnlinkedTempFiles []string `json:"unlinked_temp_files"`
	}
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	if len(out.ExpiredSessions) != 1 || out.ExpiredSessions[0] != oldID {
		t.Fatalf("expired sessions = %v, want [%s]", out.ExpiredSessions, oldID)
	}
	if len(out.UnlinkedTempFiles) != 1 || out.UnlinkedTempFiles[0] != "junk-"+strings.Repeat("f", 8) {
		t.Fatalf("unlinked files = %v", out.UnlinkedTempFiles)
	}
	if _, err := e.st.GetUpload(context.Background(), liveID); err != nil {
		t.Fatalf("live session must survive orphan cleanup: %v", err)
	}
	// The live session's staging file must still be present.
	if _, err := os.Stat(e.fs.TempPath(liveID)); err != nil {
		t.Fatalf("live staging file must survive: %v", err)
	}
}
