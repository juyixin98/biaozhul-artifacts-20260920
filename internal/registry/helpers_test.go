package registry_test

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"layer-gc/internal/db"
	"layer-gc/internal/gc"
	"layer-gc/internal/httpapi"
	"layer-gc/internal/maintenance"
	"layer-gc/internal/registry"
	"layer-gc/internal/storage"
)

// testDSN points at the acceptance database; create with:
//
//	sudo -u postgres psql -c "CREATE DATABASE registry_gc_test OWNER gcuser;"
func testDSN(t *testing.T) string {
	t.Helper()
	if v := os.Getenv("TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgres://gcuser:gcpass@127.0.0.1:5432/registry_gc_test?sslmode=disable"
}

// env is the fully wired stack for one test: each test gets its own Postgres
// schema (so tests are independent and parallelizable) and its own disk dir.
type env struct {
	t       *testing.T
	ctx     context.Context
	db      *sql.DB
	store   *storage.Store
	reg     *registry.Service
	gc      *gc.Collector
	cleaner *maintenance.Cleaner
	srv     *httpapi.Server
	ts      *httptest.Server
	dataDir string
}

func newEnv(t *testing.T) *env {
	t.Helper()
	ctx := context.Background()

	schema := "t_" + sanitize(t.Name())
	root, err := sql.Open("pgx", testDSN(t))
	if err != nil {
		t.Fatalf("open root: %v", err)
	}
	if _, err := root.ExecContext(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
		root.Close()
		t.Fatalf("drop schema (%s): %v; set TEST_DATABASE_URL or create registry_gc_test", schema, err)
	}
	if _, err := root.ExecContext(ctx, "CREATE SCHEMA "+schema); err != nil {
		root.Close()
		t.Fatalf("create schema: %v", err)
	}
	root.Close()

	dsn := testDSN(t)
	if strings.Contains(dsn, "?") {
		dsn += "&search_path=" + schema
	} else {
		dsn += "?search_path=" + schema
	}
	pgdb, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	if err := db.Migrate(ctx, pgdb); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	dir := t.TempDir()
	store, err := storage.New(dir)
	if err != nil {
		t.Fatalf("storage: %v", err)
	}
	reg := registry.New(pgdb, store)
	collector := gc.New(pgdb, store)
	cleaner := maintenance.NewCleaner(pgdb, store)
	srv := httpapi.NewServer(reg, collector, cleaner, log.New(io.Discard, "", 0))
	srv.LeaseTTL = 30 * time.Second
	ts := httptest.NewServer(srv.Routes())

	e := &env{
		t: t, ctx: ctx, db: pgdb, store: store, reg: reg, gc: collector,
		cleaner: cleaner, srv: srv, ts: ts, dataDir: dir,
	}
	t.Cleanup(func() {
		ts.Close()
		pgdb.Close()
	})
	return e
}

func sanitize(s string) string {
	r := strings.NewReplacer("/", "_", " ", "_", "=", "_", "?", "_", "-", "_")
	return strings.ToLower(r.Replace(s))
}

// ---- HTTP protocol helpers -------------------------------------------------

// startUpload begins a staged upload and returns its Location.
func (e *env) startUpload(repo string) string {
	e.t.Helper()
	resp, err := e.ts.Client().Post(e.ts.URL+"/v2/"+repo+"/blobs/uploads/", "application/octet-stream", nil)
	if err != nil {
		e.t.Fatalf("start upload: %v", err)
	}
	loc := resp.Header.Get("Location")
	resp.Body.Close()
	if resp.StatusCode != 202 {
		e.t.Fatalf("start upload status %d", resp.StatusCode)
	}
	return loc
}

// pushBlob streams content through the full upload protocol and returns the
// server-verified digest.
func (e *env) pushBlob(repo string, content []byte) string {
	e.t.Helper()
	return e.commitBlob(repo, content, digestOf(content))
}

// pushBlobBadDigest claims a digest that does not match the content and
// returns the response status/body (the publish must be rejected).
func (e *env) pushBlobBadDigest(repo string, content, claimed []byte) (int, string) {
	e.t.Helper()
	return e.commitBlobStatus(repo, content, digestOf(claimed))
}

func (e *env) commitBlob(repo string, content []byte, claim string) string {
	e.t.Helper()
	status, body := e.commitBlobStatus(repo, content, claim)
	if status != http.StatusCreated {
		e.t.Fatalf("commit blob status %d: %s", status, body)
	}
	return claim
}

func (e *env) commitBlobStatus(repo string, content []byte, claim string) (int, string) {
	e.t.Helper()
	loc := e.startUpload(repo)
	// Stream the bytes via PATCH (real chunked staging to disk).
	patchReq, _ := http.NewRequest(http.MethodPatch, e.ts.URL+loc, bytes.NewReader(content))
	patchReq.Header.Set("Content-Type", "application/octet-stream")
	presp, err := e.ts.Client().Do(patchReq)
	if err != nil {
		e.t.Fatalf("patch upload: %v", err)
	}
	pb, _ := io.ReadAll(presp.Body)
	presp.Body.Close()
	if presp.StatusCode != http.StatusAccepted {
		e.t.Fatalf("patch upload status %d: %s", presp.StatusCode, pb)
	}
	req, _ := http.NewRequest(http.MethodPut,
		e.ts.URL+loc+"?digest="+url.QueryEscape(claim), nil)
	resp, err := e.ts.Client().Do(req)
	if err != nil {
		e.t.Fatalf("commit upload: %v", err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == http.StatusCreated {
		return resp.StatusCode, resp.Header.Get("Docker-Content-Digest")
	}
	return resp.StatusCode, string(b)
}

type manifestBody struct {
	SchemaVersion int          `json:"schemaVersion"`
	MediaType     string       `json:"mediaType"`
	Config        descriptor   `json:"config"`
	Layers        []descriptor `json:"layers"`
}
type descriptor struct {
	MediaType string `json:"mediaType"`
	Digest    string `json:"digest"`
	Size      int64  `json:"size"`
}

func (e *env) buildManifest(configDigest string, layers []string) []byte {
	ld := make([]descriptor, 0, len(layers))
	for _, l := range layers {
		ld = append(ld, descriptor{
			MediaType: "application/vnd.oci.image.layer.v1.tar+gzip", Digest: l,
		})
	}
	m := manifestBody{
		SchemaVersion: 2,
		MediaType:     "application/vnd.oci.image.manifest.v1+json",
		Config:        descriptor{MediaType: "application/vnd.oci.image.config.v1+json", Digest: configDigest},
		Layers:        ld,
	}
	raw, _ := json.Marshal(m)
	return raw
}

// putManifest stores a manifest and (if tag != "") tags it; returns digest.
func (e *env) putManifest(repo, tag, configDigest string, layers []string) string {
	e.t.Helper()
	raw := e.buildManifest(configDigest, layers)
	ref := tag
	if ref == "" {
		ref = digestOf(raw)
	}
	return e.putRawManifest(repo, ref, raw)
}

func (e *env) putRawManifest(repo, ref string, raw []byte) string {
	req, _ := http.NewRequest(http.MethodPut,
		fmt.Sprintf("%s/v2/%s/manifests/%s", e.ts.URL, repo, ref), bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/vnd.oci.image.manifest.v1+json")
	resp, err := e.ts.Client().Do(req)
	if err != nil {
		e.t.Fatalf("put manifest: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		b, _ := io.ReadAll(resp.Body)
		e.t.Fatalf("put manifest status %d: %s", resp.StatusCode, b)
	}
	return resp.Header.Get("Docker-Content-Digest")
}

func (e *env) tag(repo, tag, manifestDigest string) {
	e.t.Helper()
	if err := e.reg.Tag(e.ctx, repo, tag, manifestDigest); err != nil {
		e.t.Fatalf("tag: %v", err)
	}
}

func (e *env) deleteTag(repo, tag string) {
	e.t.Helper()
	req, _ := http.NewRequest(http.MethodDelete,
		fmt.Sprintf("%s/v2/%s/manifests/%s", e.ts.URL, repo, tag), nil)
	resp, err := e.ts.Client().Do(req)
	if err != nil {
		e.t.Fatalf("delete tag: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		e.t.Fatalf("delete tag status %d", resp.StatusCode)
	}
}

func (e *env) deleteManifest(manifestDigest string) {
	e.t.Helper()
	req, _ := http.NewRequest(http.MethodDelete,
		fmt.Sprintf("%s/v2/_/manifests/%s", e.ts.URL, manifestDigest), nil)
	resp, err := e.ts.Client().Do(req)
	if err != nil {
		e.t.Fatalf("delete manifest: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		e.t.Fatalf("delete manifest status %d", resp.StatusCode)
	}
}

func (e *env) exists(d string) bool {
	ok, err := e.reg.HasBlob(e.ctx, d)
	if err != nil {
		e.t.Fatalf("has blob: %v", err)
	}
	return ok
}

// runGC via HTTP and decode the report.
func (e *env) runGC(query string) map[string]any {
	e.t.Helper()
	resp, err := e.ts.Client().Post(e.ts.URL+"/admin/gc/run"+query, "application/json", nil)
	if err != nil {
		e.t.Fatalf("run gc: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		e.t.Fatalf("gc status %d: %s", resp.StatusCode, b)
	}
	var rep map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&rep); err != nil {
		e.t.Fatalf("decode report: %v", err)
	}
	return rep
}

func (e *env) gcReport(id int64) map[string]any {
	resp, err := e.ts.Client().Get(fmt.Sprintf("%s/admin/gc/report?id=%d", e.ts.URL, id))
	if err != nil {
		e.t.Fatalf("report: %v", err)
	}
	defer resp.Body.Close()
	var rep map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&rep); err != nil {
		e.t.Fatalf("decode: %v", err)
	}
	return rep
}

func (e *env) tmpDir() string { return filepath.Join(e.dataDir, "tmp", "uploads") }

func digestOf(b []byte) string {
	return gcDigestOf(b)
}

func num(m map[string]any, k string) int {
	switch v := m[k].(type) {
	case float64:
		return int(v)
	case int:
		return v
	default:
		return -1
	}
}

func pretty(m map[string]any) string {
	b, _ := json.MarshalIndent(m, "", "  ")
	return string(b)
}

func containsReason(rs []string, sub string) bool {
	for _, r := range rs {
		if strings.Contains(r, sub) {
			return true
		}
	}
	return false
}

func waitUntil(t *testing.T, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition not met before timeout")
}
