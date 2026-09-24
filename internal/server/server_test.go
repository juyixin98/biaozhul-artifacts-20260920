package server

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"buildcache/internal/builder"
	"buildcache/internal/store"
)

type harness struct {
	t       *testing.T
	srv     *httptest.Server
	dataDir string
	client  *http.Client
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	dataDir := t.TempDir()
	sourceRoot := t.TempDir()
	st, err := store.Open(context.Background(), filepath.Join(dataDir, "cache.db"), dataDir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.ResetStaleLeases(context.Background()); err != nil {
		t.Fatal(err)
	}
	b, err := builder.New(st, dataDir, sourceRoot)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(New(b).Handler())
	t.Cleanup(func() {
		srv.Close()
		_ = st.Close()
	})
	return &harness{t: t, srv: srv, dataDir: dataDir, client: srv.Client()}
}

func (h *harness) post(path string, body any) (int, map[string]any, []byte) {
	h.t.Helper()
	buf, _ := json.Marshal(body)
	resp, err := h.client.Post(h.srv.URL+path, "application/json", bytes.NewReader(buf))
	if err != nil {
		h.t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	return resp.StatusCode, m, raw
}

func (h *harness) get(path string) (int, http.Header, []byte) {
	h.t.Helper()
	resp, err := h.client.Get(h.srv.URL + path)
	if err != nil {
		h.t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, resp.Header, raw
}

// getJSON is like get but decodes a JSON object body.
func (h *harness) getJSON(path string) (int, map[string]any) {
	h.t.Helper()
	status, _, raw := h.get(path)
	var m map[string]any
	if len(bytes.TrimSpace(raw)) > 0 {
		_ = json.Unmarshal(raw, &m)
	}
	return status, m
}

// buildBody constructs a request for the shell toolchain that writes content
// into the declared artifact.
func buildBody(label, content string) map[string]any {
	return map[string]any{
		"toolchain":       "shell",
		"version_command": []string{"sh", "-c", "echo posix-sh 1.0"},
		"command": []string{
			"sh", "-c", "printf '%s' \"$2\" > \"$1\"; chmod +x \"$1\"", label, "{artifact}", "{args}",
		},
		"args":   []string{content},
		"target": map[string]string{},
		"env":    map[string]string{},
		"sources": []map[string]any{{
			"path":       "input-" + label + ".txt",
			"inline_b64": base64.StdEncoding.EncodeToString([]byte(content)),
		}},
		"artifact":        "out-" + label + ".bin",
		"timeout_seconds": 30,
		"wait_seconds":    60,
	}
}

func TestHTTP_MissHitStatusCodes(t *testing.T) {
	h := newHarness(t)

	status, body, _ := h.post("/builds", buildBody("g", "payload"))
	if status != http.StatusCreated {
		t.Fatalf("cold build status=%d want 201", status)
	}
	if body["hit"] != false || body["status"] != "succeeded" {
		t.Fatalf("cold build body wrong: %v", body)
	}
	key, _ := body["key"].(string)
	sha, _ := body["artifact_sha"].(string)
	if len(sha) != 64 {
		t.Fatalf("bad artifact sha %q", sha)
	}

	status, body, _ = h.post("/builds", buildBody("g", "payload"))
	if status != http.StatusOK {
		t.Fatalf("warm build status=%d want 200", status)
	}
	if body["hit"] != true || body["key"] != key || body["artifact_sha"] != sha {
		t.Fatalf("warm build must be a hit: %v", body)
	}

	// Changed content -> new key, miss again.
	status, body2, _ := h.post("/builds", buildBody("g", "payload2"))
	if status != http.StatusCreated {
		t.Fatalf("changed build status=%d want 201", status)
	}
	if body2["key"] == key {
		t.Fatal("changed content must change key")
	}
}

func TestHTTP_ArtifactDownloadAndTamper(t *testing.T) {
	h := newHarness(t)
	_, body, _ := h.post("/builds", buildBody("d", "download-me"))
	key, _ := body["key"].(string)
	sha, _ := body["artifact_sha"].(string)

	status, hdr, raw := h.get("/builds/" + key + "/artifact")
	if status != http.StatusOK || string(raw) != "download-me" {
		t.Fatalf("artifact download status=%d body=%q", status, raw)
	}
	if hdr.Get("X-Artifact-SHA256") != sha {
		t.Fatalf("artifact digest header wrong: %q", hdr.Get("X-Artifact-SHA256"))
	}

	// Tamper with the blob on disk, then both the build endpoint and artifact
	// download must quarantine it; the entry heals on the next build.
	blob := filepath.Join(h.dataDir, "blobs", sha[:2], sha)
	if err := os.WriteFile(blob, []byte(strings.Repeat("X", len("download-me"))), 0o755); err != nil {
		t.Fatal(err)
	}
	status, _, raw = h.get("/builds/" + key + "/artifact")
	if status != http.StatusGone {
		t.Fatalf("tampered artifact status=%d want 410", status)
	}
	var qb map[string]any
	_ = json.Unmarshal(raw, &qb)
	if qb["reason"] != "hash_mismatch" {
		t.Fatalf("quarantine reason=%v want hash_mismatch", qb["reason"])
	}

	status, evBody := h.getJSON("/builds/" + key + "/quarantine")
	if status != http.StatusOK {
		t.Fatalf("quarantine status=%d", status)
	}
	evs := evBody["events"].([]any)
	if len(evs) != 1 {
		t.Fatalf("want 1 quarantine event, got %v", evs)
	}

	// Rebuild heals the entry.
	status, healed, _ := h.post("/builds", buildBody("d", "download-me"))
	if status != http.StatusCreated || healed["status"] != "succeeded" {
		t.Fatalf("rebuild did not heal: %d %v", status, healed)
	}
	status, _, raw = h.get("/builds/" + key + "/artifact")
	if status != http.StatusOK || string(raw) != "download-me" {
		t.Fatalf("artifact not restored: status=%d body=%q", status, raw)
	}
}

func TestHTTP_ArtifactMissingIsQuarantined(t *testing.T) {
	h := newHarness(t)
	_, body, _ := h.post("/builds", buildBody("m", "x"))
	key, _ := body["key"].(string)
	sha, _ := body["artifact_sha"].(string)
	if err := os.Remove(filepath.Join(h.dataDir, "blobs", sha[:2], sha)); err != nil {
		t.Fatal(err)
	}
	status, qbody := h.getJSON("/builds/" + key + "/artifact")
	if status != http.StatusGone || qbody["reason"] != "artifact_missing" {
		t.Fatalf("missing artifact status=%d reason=%v", status, qbody["reason"])
	}
}

func TestHTTP_AuditKeyMissingVsEmptyAndEnvBinding(t *testing.T) {
	h := newHarness(t)

	withEmpty := map[string]any{
		"toolchain":       "shell",
		"version_command": []string{"sh", "-c", "echo posix-sh 1.0"},
		"command":         []string{"true"},
		"target":          map[string]string{},
		"env":             map[string]string{},
		"sources":         []map[string]any{{"path": "opt.cfg", "inline_b64": ""}},
		"artifact":        "out.bin",
	}
	missing := map[string]any{
		"toolchain":       "shell",
		"version_command": []string{"sh", "-c", "echo posix-sh 1.0"},
		"command":         []string{"true"},
		"target":          map[string]string{},
		"env":             map[string]string{},
		"sources":         []map[string]any{{"path": "opt.cfg", "optional": true}},
		"artifact":        "out.bin",
	}
	st1, b1, _ := h.post("/audit/key", withEmpty)
	st2, b2, _ := h.post("/audit/key", missing)
	if st1 != 200 || st2 != 200 {
		t.Fatalf("audit statuses %d %d", st1, st2)
	}
	if b1["key"] == b2["key"] {
		t.Fatal("empty and missing file must audit to different keys")
	}
	// The manifest must literally distinguish present/empty vs absent.
	src1 := b1["source_manifest"].([]any)[0].(map[string]any)
	if src1["present"] != true || src1["digest"] !=
		"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" {
		t.Fatalf("empty manifest wrong: %v", src1)
	}
	src2 := b2["source_manifest"].([]any)[0].(map[string]any)
	if src2["present"] != false || src2["digest"] != "" {
		t.Fatalf("missing manifest wrong: %v", src2)
	}

	// Declared env changes the audit key; the canonical material echoes it.
	withEnv := withEmpty
	withEnv["env"] = map[string]string{"FOO": "bar"}
	_, b3, _ := h.post("/audit/key", withEnv)
	if b3["key"] == b1["key"] {
		t.Fatal("declared env must participate in the key")
	}
}

func TestHTTP_BadRequests(t *testing.T) {
	h := newHarness(t)

	status, _, _ := h.post("/builds", map[string]any{"toolchain": "shell"})
	if status != http.StatusBadRequest {
		t.Fatalf("incomplete request status=%d want 400", status)
	}

	// Unknown fields are rejected.
	bad := buildBody("x", "y")
	bad["bogus"] = 1
	status, _, _ = h.post("/builds", bad)
	if status != http.StatusBadRequest {
		t.Fatalf("unknown field status=%d want 400", status)
	}

	// Bad base64 rejected.
	badB64 := buildBody("x", "y")
	badB64["sources"] = []map[string]any{{"path": "a", "inline_b64": "###notb64###"}}
	status, body, _ := h.post("/builds", badB64)
	if status != http.StatusBadRequest {
		t.Fatalf("bad b64 status=%d want 400", status)
	}
	if !strings.Contains(fmt.Sprint(body), "invalid_inline_b64") {
		t.Fatalf("unexpected error body: %v", body)
	}
}

func TestHTTP_KeyShapeAndNotFound(t *testing.T) {
	h := newHarness(t)
	status, _ := h.getJSON("/builds/not-a-key")
	if status != http.StatusBadRequest {
		t.Fatalf("bad key shape status=%d want 400", status)
	}
	status, _ = h.getJSON("/builds/bck1-" + strings.Repeat("a", 64))
	if status != http.StatusNotFound {
		t.Fatalf("unknown key status=%d want 404", status)
	}
	status, _ = h.getJSON("/builds/bck1-" + strings.Repeat("a", 64) + "/artifact")
	if status != http.StatusNotFound {
		t.Fatalf("unknown artifact status=%d want 404", status)
	}
}

func TestHTTP_Health(t *testing.T) {
	h := newHarness(t)
	status, body := h.getJSON("/healthz")
	if status != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("health=%d %v", status, body)
	}
}

func TestHTTP_FailedBuildReportedHonestly(t *testing.T) {
	h := newHarness(t)
	body := buildBody("fail", "ignored")
	body["command"] = []string{"sh", "-c", "echo boom >&2; exit 7", "fail"}
	status, resp, _ := h.post("/builds", body)
	if status != http.StatusCreated {
		t.Fatalf("failed build HTTP status=%d want 201 (build ran)", status)
	}
	if resp["status"] != "failed" || resp["exit_code"] != float64(7) {
		t.Fatalf("failure not recorded honestly: %v", resp)
	}
	if !strings.Contains(fmt.Sprint(resp["stderr"]), "boom") {
		t.Fatalf("stderr not surfaced: %v", resp)
	}
	key, _ := resp["key"].(string)

	// Artifact endpoint refuses to serve a failed entry.
	status, _ = h.getJSON("/builds/" + key + "/artifact")
	if status != http.StatusConflict {
		t.Fatalf("failed artifact status=%d want 409", status)
	}
	// GET entry shows failed.
	status, entry := h.getJSON("/builds/" + key)
	if status != http.StatusOK || entry["status"] != "failed" || entry["artifact_sha"] != "" {
		t.Fatalf("failed entry wrong: %v", entry)
	}
}
