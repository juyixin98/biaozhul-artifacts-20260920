package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"deltaupdate/internal/delta"
)

func newTestServer(t *testing.T) (*Server, string, string) {
	t.Helper()
	root := t.TempDir()
	cache := filepath.Join(root, "cache")
	work := filepath.Join(root, "work")
	s, err := New(Config{CacheDir: cache, WorkDir: work, MaxUploadBytes: 20 << 20})
	if err != nil {
		t.Fatal(err)
	}
	return s, cache, work
}

func do(t *testing.T, h http.Handler, method, path string, body any, headers map[string]string) (*http.Response, []byte) {
	t.Helper()
	var rdr io.Reader
	switch v := body.(type) {
	case []byte:
		rdr = bytes.NewReader(v)
	case string:
		rdr = strings.NewReader(v)
	case nil:
	default:
		b, _ := json.Marshal(v)
		rdr = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, rdr)
	for k, val := range headers {
		req.Header.Set(k, val)
	}
	if body != nil {
		if _, ok := body.([]byte); ok {
			req.Header.Set("Content-Type", "application/octet-stream")
		} else if _, ok := body.(string); !ok {
			req.Header.Set("Content-Type", "application/json")
		}
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	res := rec.Result()
	raw, _ := io.ReadAll(res.Body)
	return res, raw
}

func uploadArtifact(t *testing.T, h http.Handler, b []byte) string {
	res, raw := do(t, h, "POST", "/v1/artifacts", b, nil)
	if res.StatusCode != 201 {
		t.Fatalf("upload status %d: %s", res.StatusCode, raw)
	}
	var ur uploadResp
	json.Unmarshal(raw, &ur)
	if ur.Hash != delta.DigestBytes(b) {
		t.Fatal("upload hash mismatch")
	}
	return ur.Hash
}

func TestHealthAndFullFlow(t *testing.T) {
	s, _, work := newTestServer(t)
	h := s.Handler()

	res, _ := do(t, h, "GET", "/v1/health", nil, nil)
	if res.StatusCode != 200 {
		t.Fatal("health failed")
	}

	oldB := bytes.Repeat([]byte("old-artifact-"), 1500)
	newB := append([]byte{}, oldB...)
	newB = append(newB[:2000], append([]byte("<<INSERTED-REGION>>"), newB[2000:]...)...)
	oldH := uploadArtifact(t, h, oldB)
	newH := uploadArtifact(t, h, newB)

	// Generate delta.
	res, raw := do(t, h, "POST", "/v1/deltas", map[string]any{
		"oldRef": oldH, "newRef": newH, "blockSize": 512,
	}, nil)
	if res.StatusCode != 200 {
		t.Fatalf("delta status %d: %s", res.StatusCode, raw)
	}
	var dr deltaResp
	json.Unmarshal(raw, &dr)
	if dr.Patch.OldSum != oldH || dr.Patch.NewSum != newH {
		t.Fatal("delta bindings wrong")
	}
	copies := 0
	for _, op := range dr.Patch.Ops {
		if op.Type == delta.OpCopy {
			copies++
		}
	}
	if copies < 30 {
		t.Fatalf("expected high block reuse across shifted boundary, copies=%d", copies)
	}

	// Seed a target with the OLD bytes under work dir.
	target := "builds/app1/current.bin"
	res, raw = do(t, h, "PUT", "/v1/work/"+target, oldB, nil)
	if res.StatusCode != 200 {
		t.Fatalf("seed status %d: %s", res.StatusCode, raw)
	}
	if _, err := os.Stat(filepath.Join(work, target)); err != nil {
		t.Fatal("seed file missing:", err)
	}

	// Apply.
	res, raw = do(t, h, "POST", "/v1/apply", map[string]any{
		"target": target, "patch": dr.Patch,
	}, nil)
	if res.StatusCode != 200 {
		t.Fatalf("apply status %d: %s", res.StatusCode, raw)
	}
	var ar applyResp
	json.Unmarshal(raw, &ar)
	if !ar.Applied || ar.NewDigest != newH {
		t.Fatalf("apply response wrong: %s", raw)
	}

	// Inspect via work API.
	res, raw = do(t, h, "GET", "/v1/work/"+target, nil, nil)
	if res.StatusCode != 200 {
		t.Fatalf("work get %d: %s", res.StatusCode, raw)
	}
	var info map[string]any
	json.Unmarshal(raw, &info)
	if info["sha256"] != newH {
		t.Fatal("final digest mismatch")
	}

	// Re-apply -> idempotent, applied=false.
	res, raw = do(t, h, "POST", "/v1/apply", map[string]any{
		"target": target, "patch": dr.Patch,
	}, nil)
	if res.StatusCode != 200 {
		t.Fatalf("reapply status %d: %s", res.StatusCode, raw)
	}
	json.Unmarshal(raw, &ar)
	if ar.Applied {
		t.Fatal("reapply should report applied=false")
	}
}

func TestApplyWrongBaseOverHTTP(t *testing.T) {
	s, _, _ := newTestServer(t)
	h := s.Handler()
	oldB := bytes.Repeat([]byte("AAAA"), 2000)
	newB := bytes.Repeat([]byte("BBBB"), 2000)
	oldH := uploadArtifact(t, h, oldB)
	newH := uploadArtifact(t, h, newB)
	res, raw := do(t, h, "POST", "/v1/deltas", map[string]any{
		"oldRef": oldH, "newRef": newH, "blockSize": 256,
	}, nil)
	var dr deltaResp
	json.Unmarshal(raw, &dr)

	// Seed DIFFERENT content into the target.
	other := bytes.Repeat([]byte("CCCC"), 2000)
	do(t, h, "PUT", "/v1/work/t.bin", other, nil)
	res, raw = do(t, h, "POST", "/v1/apply", map[string]any{
		"target": "t.bin", "patch": dr.Patch,
	}, nil)
	if res.StatusCode != http.StatusConflict {
		t.Fatalf("want 409, got %d: %s", res.StatusCode, raw)
	}
	if !strings.Contains(string(raw), "wrong_base") {
		t.Fatalf("missing error code: %s", raw)
	}
	// Old content intact.
	_, raw = do(t, h, "GET", "/v1/work/t.bin", nil, nil)
	var info map[string]any
	json.Unmarshal(raw, &info)
	if info["sha256"] != delta.DigestBytes(other) {
		t.Fatal("wrong-base mutated target")
	}
}

func TestApplyCorruptPatchOverHTTP(t *testing.T) {
	s, _, _ := newTestServer(t)
	h := s.Handler()
	oldB := bytes.Repeat([]byte("old"), 2000)
	newB := bytes.Repeat([]byte("new"), 2000)
	oldH := uploadArtifact(t, h, oldB)
	newH := uploadArtifact(t, h, newB)
	_, raw := do(t, h, "POST", "/v1/deltas", map[string]any{
		"oldRef": oldH, "newRef": newH, "blockSize": 256,
	}, nil)
	var dr deltaResp
	json.Unmarshal(raw, &dr)

	// Tamper metadata while keeping the stale patchSum.
	bad := *dr.Patch
	bad.NewSum = delta.DigestBytes([]byte("totally different content"))
	do(t, h, "PUT", "/v1/work/t.bin", oldB, nil)
	res, raw := do(t, h, "POST", "/v1/apply", map[string]any{
		"target": "t.bin", "patch": &bad,
	}, nil)
	if res.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("want 422, got %d: %s", res.StatusCode, raw)
	}
}

func TestSimulatedSpaceShortageOverHTTP(t *testing.T) {
	s, _, _ := newTestServer(t)
	h := s.Handler()
	oldB := make([]byte, 4096)
	newB := make([]byte, 300_000)
	oldH := uploadArtifact(t, h, oldB)
	newH := uploadArtifact(t, h, newB)
	_, raw := do(t, h, "POST", "/v1/deltas", map[string]any{
		"oldRef": oldH, "newRef": newH, "blockSize": 512,
	}, nil)
	var dr deltaResp
	json.Unmarshal(raw, &dr)
	do(t, h, "PUT", "/v1/work/t.bin", oldB, nil)
	res, raw := do(t, h, "POST", "/v1/apply", map[string]any{
		"target": "t.bin", "patch": dr.Patch, "simFreeBytes": 100,
	}, nil)
	if res.StatusCode != http.StatusInsufficientStorage {
		t.Fatalf("want 507, got %d: %s", res.StatusCode, raw)
	}
}

func TestPathTraversalRejected(t *testing.T) {
	s, _, _ := newTestServer(t)
	h := s.Handler()
	for _, p := range []string{
		"/v1/work/../etc/passwd",
		"/v1/work/../../etc/passwd",
		"/v1/work/a/../../escape",
	} {
		res, _ := do(t, h, "GET", p, nil, nil)
		if res.StatusCode == 200 {
			t.Fatalf("traversal path %q was not rejected", p)
		}
	}
}

func TestCacheWorkSeparation(t *testing.T) {
	root := t.TempDir()
	cache := filepath.Join(root, "cache")
	work := filepath.Join(root, "work")
	s, err := New(Config{CacheDir: cache, WorkDir: work})
	if err != nil {
		t.Fatal(err)
	}
	// Upload goes ONLY to cache; no artifact appears under work.
	uploadArtifact(t, s.Handler(), []byte("cached-bytes"))
	wentries, _ := os.ReadDir(work)
	if len(wentries) != 0 {
		t.Fatalf("upload leaked into work dir: %v", wentries)
	}
	// Seed goes ONLY to work.
	do(t, s.Handler(), "PUT", "/v1/work/w.bin", []byte("work-bytes"), nil)
	if found := findFile(cache, "w.bin"); found {
		t.Fatal("work file leaked into cache")
	}
}

func findFile(root, name string) bool {
	found := false
	_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err == nil && info != nil && info.Name() == name {
			found = true
		}
		return nil
	})
	return found
}

func TestSameDirRejected(t *testing.T) {
	d := t.TempDir()
	if _, err := New(Config{CacheDir: d, WorkDir: d}); err == nil {
		t.Fatal("expected error when cache==work")
	}
}
