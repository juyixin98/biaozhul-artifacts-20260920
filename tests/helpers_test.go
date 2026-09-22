package tests_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"image/color"
	"io"
	"net/http"
	"testing"

	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/testutil"
)

func decodeJSONBody(h *testutil.Harness, method, path, key string, dst any) error {
	req, _ := http.NewRequestWithContext(h.Ctx, method, h.BaseURL()+path, nil)
	if key != "" {
		req.Header.Set("X-API-Key", key)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		return fmt.Errorf("status %d: %s", resp.StatusCode, raw)
	}
	return json.Unmarshal(raw, dst)
}

// seedComposition creates a project (as key owner), uploads two real PNG
// layers and freezes one valid version. Returns project, composition and
// version ids.
func seedComposition(t *testing.T, h *testutil.Harness, key string) (uuid.UUID, uuid.UUID, uuid.UUID) {
	t.Helper()
	proj := h.CreateProject(key, "proj")
	h.UploadAsset(key, proj, "bg.png", testutil.MakePNG(40, 20, color.RGBA{20, 80, 160, 255}))
	h.UploadAsset(key, proj, "fg.png", testutil.MakePNG(20, 10, color.RGBA{230, 200, 40, 160}))

	st, body := h.Do("POST", "/projects/"+proj.String()+"/compositions", key,
		map[string]any{"name": "comp", "width": 40, "height": 20})
	if st != http.StatusCreated {
		t.Fatalf("create comp: %d %v", st, body)
	}
	compID := h.AsID(body, "id")

	manifest := map[string]any{
		"width":  40,
		"height": 20,
		"layers": []map[string]any{
			{"id": "bg", "asset": "bg.png", "x": 0, "y": 0},
			{"id": "fg", "asset": "fg.png", "x": 10, "y": 5},
		},
	}
	st, body = h.FreezeManifest(key, compID, manifest)
	if st != http.StatusCreated {
		t.Fatalf("freeze: %d %v", st, body)
	}
	return proj, compID, h.AsID(body, "id")
}

// freezeBadManifest posts a manifest expected to be rejected (400).
func freezeBadManifest(t *testing.T, h *testutil.Harness, key string, compID uuid.UUID, manifest any) {
	t.Helper()
	st, body := h.FreezeManifest(key, compID, manifest)
	if st != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %v", st, body)
	}
}

// pngAt downloads a frame and returns its raw bytes.
func pngAt(h *testutil.Harness, path string) []byte {
	req, _ := http.NewRequestWithContext(h.Ctx, "GET", h.BaseURL()+path, nil)
	req.Header.Set("X-API-Key", testutil.AdminKey)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.T.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		h.T.Fatalf("get png %s: %d", path, resp.StatusCode)
	}
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, resp.Body); err != nil {
		h.T.Fatal(err)
	}
	return buf.Bytes()
}
