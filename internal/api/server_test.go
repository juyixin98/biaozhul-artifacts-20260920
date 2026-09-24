package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.example.com/ocimultipick/internal/api"
	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/fixture"
	"github.example.com/ocimultipick/internal/oci"
	"github.example.com/ocimultipick/internal/store"
)

type harness struct {
	server *httptest.Server
	blobs  *blobstore.Store
	db     *store.Store
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	dir := t.TempDir()
	blobs := blobstore.New(filepath.Join(dir, "registry"))
	db, err := store.Open(context.Background(), filepath.Join(dir, "data.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	h := &harness{
		blobs: blobs,
		db:    db,
	}
	h.server = httptest.NewServer(api.NewServer(blobs, db).Router())
	t.Cleanup(h.server.Close)
	return h
}

func (h *harness) seedTag(t *testing.T, repo, tag, digest, mediaType string) {
	t.Helper()
	if err := h.db.UpsertTag(context.Background(), store.Tag{
		Repository: repo, Tag: tag, Digest: digest, MediaType: mediaType,
	}); err != nil {
		t.Fatal(err)
	}
}

func (h *harness) doJSON(t *testing.T, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, h.server.URL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

// buildTwoArchIndex creates a real two-arch repository and returns the root
// index digest.
func (h *harness) buildTwoArchIndex(t *testing.T, repo string) string {
	t.Helper()
	b := fixture.NewBuilder(h.blobs, repo)

	mk := func(name, arch, payload string) oci.Descriptor {
		content := fixture.LayerContent(name, []byte(payload))
		layer, err := b.AddLayer(name, content)
		if err != nil {
			t.Fatal(err)
		}
		cfg, err := b.AddConfig("linux", arch, "", []string{fixture.DiffIDUncompressed(content)})
		if err != nil {
			t.Fatal(err)
		}
		m, err := b.AddManifest(cfg, []oci.Descriptor{layer})
		if err != nil {
			t.Fatal(err)
		}
		m.Platform = &oci.Platform{OS: "linux", Architecture: arch}
		return m
	}
	amd := mk("amd", "amd64", "x86_64 payload")
	arm := mk("arm", "arm64", "aarch64 payload")
	root, _, err := b.AddIndex([]oci.Descriptor{amd, arm})
	if err != nil {
		t.Fatal(err)
	}
	return root.Digest
}

func TestResolveByTagSelectsArchitecture(t *testing.T) {
	h := newHarness(t)
	root := h.buildTwoArchIndex(t, "demo/app")
	h.seedTag(t, "demo/app", "latest", root, oci.MediaTypeImageIndex)

	status, body := h.doJSON(t, http.MethodPost, "/v1/repos/demo/app/resolve", map[string]any{
		"reference": "latest",
		"platform":  map[string]string{"os": "linux", "architecture": "arm64"},
	})
	if status != http.StatusCreated {
		t.Fatalf("status = %d body=%v", status, body)
	}
	if body["status"] != "succeeded" {
		t.Fatalf("status field = %v, error=%v", body["status"], body["error"])
	}
	resolved, _ := body["resolved"].(map[string]any)
	manifest, _ := resolved["manifest"].(map[string]any)
	if manifest["digest"] == "" || manifest["digest"] == root {
		t.Fatalf("expected a leaf manifest digest distinct from the index, got %v", manifest["digest"])
	}
	chain, _ := body["dependencyChain"].([]any)
	if len(chain) != 4 {
		t.Fatalf("dependencyChain length = %d, want 4", len(chain))
	}
}

func TestResolveByDigestIgnoresTags(t *testing.T) {
	h := newHarness(t)
	root := h.buildTwoArchIndex(t, "demo/app")

	status, body := h.doJSON(t, http.MethodPost, "/v1/repos/demo/app/resolve", map[string]any{
		"reference": root,
		"platform":  map[string]string{"os": "linux", "architecture": "amd64"},
	})
	if status != http.StatusCreated {
		t.Fatalf("status=%d body=%v", status, body)
	}
}

func TestAmbiguousReturns409(t *testing.T) {
	h := newHarness(t)
	b := fixture.NewBuilder(h.blobs, "demo/amb")
	mk := func(payload string) oci.Descriptor {
		content := fixture.LayerContent("x", []byte(payload))
		layer, _ := b.AddLayer("x", content)
		cfg, _ := b.AddConfig("linux", "amd64", "", []string{fixture.DiffIDUncompressed(content)})
		m, _ := b.AddManifest(cfg, []oci.Descriptor{layer})
		m.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}
		return m
	}
	root, _, _ := b.AddIndex([]oci.Descriptor{mk("A"), mk("B")})
	h.seedTag(t, "demo/amb", "latest", root.Digest, oci.MediaTypeImageIndex)

	status, body := h.doJSON(t, http.MethodPost, "/v1/repos/demo/amb/resolve", map[string]any{
		"reference": "latest",
		"platform":  map[string]string{"os": "linux", "architecture": "amd64"},
	})
	if status != http.StatusConflict {
		t.Fatalf("status = %d, want 409; body=%v", status, body)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["code"] != "platform_ambiguous" {
		t.Fatalf("error code = %v", errObj["code"])
	}
	// Even the failed attempt must be retrievable as a task.
	if body["id"] == "" {
		t.Fatal("failed resolve must still return a task id")
	}
}

func TestBadConfigPlatformReturns422(t *testing.T) {
	h := newHarness(t)
	b := fixture.NewBuilder(h.blobs, "demo/bad")
	content := fixture.LayerContent("m", []byte("i386"))
	layer, _ := b.AddLayer("m", content)
	// Config honestly says linux/386 while the index advertises linux/arm64.
	cfg, _ := b.AddConfig("linux", "386", "", []string{fixture.DiffIDUncompressed(content)})
	m, _ := b.AddManifest(cfg, []oci.Descriptor{layer})
	m.Platform = &oci.Platform{OS: "linux", Architecture: "arm64"}
	root, _, _ := b.AddIndex([]oci.Descriptor{m})
	h.seedTag(t, "demo/bad", "latest", root.Digest, oci.MediaTypeImageIndex)

	status, body := h.doJSON(t, http.MethodPost, "/v1/repos/demo/bad/resolve", map[string]any{
		"reference": "latest",
		"platform":  map[string]string{"os": "linux", "architecture": "arm64"},
	})
	if status != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d want 422 body=%v", status, body)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["code"] != "config_platform_mismatch" {
		t.Fatalf("code = %v", errObj["code"])
	}
}

func TestMissingLayerReturns404(t *testing.T) {
	h := newHarness(t)
	b := fixture.NewBuilder(h.blobs, "demo/missing")
	content := fixture.LayerContent("m", []byte("ghost"))
	layer, _ := b.AddLayer("m", content)
	cfg, _ := b.AddConfig("linux", "amd64", "", []string{fixture.DiffIDUncompressed(content)})
	m, _ := b.AddManifest(cfg, []oci.Descriptor{layer})
	if err := b.DeleteBlob(layer); err != nil {
		t.Fatal(err)
	}
	m.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}
	root, _, _ := b.AddIndex([]oci.Descriptor{m})
	h.seedTag(t, "demo/missing", "latest", root.Digest, oci.MediaTypeImageIndex)

	status, body := h.doJSON(t, http.MethodPost, "/v1/repos/demo/missing/resolve", map[string]any{
		"reference": "latest",
		"platform":  map[string]string{"os": "linux", "architecture": "amd64"},
	})
	if status != http.StatusNotFound {
		t.Fatalf("status = %d want 404 body=%v", status, body)
	}
	errObj, _ := body["error"].(map[string]any)
	if errObj["code"] != "blob_missing" {
		t.Fatalf("code = %v", errObj["code"])
	}
}

// TestTagMoveKeepsOldTaskBound is the central mutability guarantee: once a
// task has resolved a tag to a digest, later moving the tag must not change
// what the stored task is bound to or what its dependency chain references.
func TestTagMoveKeepsOldTaskBound(t *testing.T) {
	h := newHarness(t)
	repo := "demo/move"
	rootV1 := h.buildTwoArchIndex(t, repo)
	h.seedTag(t, repo, "latest", rootV1, oci.MediaTypeImageIndex)

	// Resolve against latest: task must bind to rootV1.
	status, body1 := h.doJSON(t, http.MethodPost, "/v1/repos/"+repo+"/resolve", map[string]any{
		"reference": "latest",
		"platform":  map[string]string{"os": "linux", "architecture": "amd64"},
	})
	if status != http.StatusCreated {
		t.Fatalf("first resolve status=%d body=%v", status, body1)
	}
	taskID, _ := body1["id"].(string)
	ref1, _ := body1["reference"].(map[string]any)
	if ref1["resolvedDigest"] != rootV1 {
		t.Fatalf("first task bound to %v, want %s", ref1["resolvedDigest"], rootV1)
	}

	// Build a genuinely different second index and move the tag.
	b := fixture.NewBuilder(h.blobs, repo)
	content := fixture.LayerContent("v2", []byte("entirely rebuilt release"))
	layer, _ := b.AddLayer("v2", content)
	cfg, _ := b.AddConfig("linux", "amd64", "", []string{fixture.DiffIDUncompressed(content)})
	m2, _ := b.AddManifest(cfg, []oci.Descriptor{layer})
	m2.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}
	rootV2Desc, _, _ := b.AddIndex([]oci.Descriptor{m2})
	rootV2 := rootV2Desc.Digest

	status, moved := h.doJSON(t, http.MethodPut, "/v1/repos/"+repo+"/tags/latest", map[string]any{
		"digest": rootV2,
	})
	if status != http.StatusOK {
		t.Fatalf("put tag status=%d body=%v", status, moved)
	}

	// A new resolve follows the moved tag to v2.
	status, body2 := h.doJSON(t, http.MethodPost, "/v1/repos/"+repo+"/resolve", map[string]any{
		"reference": "latest",
		"platform":  map[string]string{"os": "linux", "architecture": "amd64"},
	})
	if status != http.StatusCreated {
		t.Fatalf("second resolve status=%d body=%v", status, body2)
	}
	ref2, _ := body2["reference"].(map[string]any)
	if ref2["resolvedDigest"] != rootV2 || rootV2 == rootV1 {
		t.Fatalf("new task should bind moved tag to v2 (%s), got %v", rootV2, ref2["resolvedDigest"])
	}

	// The original task still resolves to v1 and carries v1's full chain.
	status, oldTask := h.doJSON(t, http.MethodGet, "/v1/repos/"+repo+"/tasks/"+taskID, nil)
	if status != http.StatusOK {
		t.Fatalf("get old task status=%d body=%v", status, oldTask)
	}
	refOld, _ := oldTask["reference"].(map[string]any)
	if refOld["resolvedDigest"] != rootV1 {
		t.Fatalf("old task rebound to %v after tag move; must stay %s", refOld["resolvedDigest"], rootV1)
	}
	chain, _ := oldTask["dependencyChain"].([]any)
	first, _ := chain[0].(map[string]any)
	if first["digest"] != rootV1 {
		t.Fatalf("old task chain head = %v, want %s", first["digest"], rootV1)
	}
}

func TestUnknownTagReturns404(t *testing.T) {
	h := newHarness(t)
	status, body := h.doJSON(t, http.MethodPost, "/v1/repos/demo/app/resolve", map[string]any{
		"reference": "does-not-exist",
		"platform":  map[string]string{"os": "linux", "architecture": "amd64"},
	})
	if status != http.StatusNotFound {
		t.Fatalf("status=%d body=%v", status, body)
	}
}

func TestHealth(t *testing.T) {
	h := newHarness(t)
	status, body := h.doJSON(t, http.MethodGet, "/healthz", nil)
	if status != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("health = %d %v", status, body)
	}
}
