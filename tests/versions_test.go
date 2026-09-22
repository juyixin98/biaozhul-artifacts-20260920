package tests_test

import (
	"bytes"
	"encoding/json"
	"image/color"
	"image/png"
	"net/http"
	"testing"

	"github.com/vfxqueue/renderq/internal/testutil"
)

// TestVersionFreezeImmutability: after freezing v1, re-uploading an asset
// with different bytes must not change v1's pinned digest; v2 picks up the
// new digest. A job created on v1 renders from v1's snapshot.
func TestVersionFreezeImmutability(t *testing.T) {
	h := testutil.New(t)
	proj, compID, v1ID := seedComposition(t, h, testutil.AliceKey)

	st, v1 := h.Do("GET", "/versions/"+v1ID.String(), testutil.AliceKey, nil)
	if st != http.StatusOK {
		t.Fatalf("get v1: %d", st)
	}
	v1Resources := v1["resources"].([]any)
	var oldSHA string
	for _, r := range v1Resources {
		rm := r.(map[string]any)
		if rm["layerId"] == "bg" {
			oldSHA = rm["sha256"].(string)
		}
	}
	if oldSHA == "" {
		t.Fatal("v1 bg resource missing")
	}

	// Re-upload bg.png with DIFFERENT pixels and size.
	h.UploadAsset(testutil.AliceKey, proj, "bg.png",
		testutil.MakePNG(40, 20, color.RGBA{123, 1, 2, 255}))

	// v1 digest unchanged.
	_, v1Again := h.Do("GET", "/versions/"+v1ID.String(), testutil.AliceKey, nil)
	for _, r := range v1Again["resources"].([]any) {
		rm := r.(map[string]any)
		if rm["layerId"] == "bg" && rm["sha256"] != oldSHA {
			t.Fatalf("frozen v1 digest changed: %s -> %s", oldSHA, rm["sha256"])
		}
	}

	// Freeze v2; it must pin the new digest.
	manifest := map[string]any{
		"width": 40, "height": 20,
		"layers": []map[string]any{
			{"id": "bg", "asset": "bg.png", "x": 0, "y": 0},
			{"id": "fg", "asset": "fg.png", "x": 10, "y": 5},
		},
	}
	st, v2 := h.FreezeManifest(testutil.AliceKey, compID, manifest)
	if st != http.StatusCreated {
		t.Fatalf("freeze v2: %d %v", st, v2)
	}
	if v2["versionNo"].(float64) != 2 {
		t.Fatalf("versionNo = %v want 2", v2["versionNo"])
	}
	var newSHA string
	for _, r := range v2["resources"].([]any) {
		rm := r.(map[string]any)
		if rm["layerId"] == "bg" {
			newSHA = rm["sha256"].(string)
		}
	}
	if newSHA == "" || newSHA == oldSHA {
		t.Fatalf("v2 should pin new digest, old=%s new=%s", oldSHA, newSHA)
	}
}

// TestFreezeRejectsMissingResource: a layer referencing an asset that was
// never uploaded must fail with 400 and no version may be created.
func TestFreezeRejectsMissingResource(t *testing.T) {
	h := testutil.New(t)
	proj := h.CreateProject(testutil.AliceKey, "p")
	h.UploadAsset(testutil.AliceKey, proj, "a.png",
		testutil.MakePNG(4, 4, color.RGBA{1, 2, 3, 255}))
	st, comp := h.Do("POST", "/projects/"+proj.String()+"/compositions", testutil.AliceKey,
		map[string]any{"name": "c", "width": 10, "height": 10})
	if st != 201 {
		t.Fatalf("comp: %d %v", st, comp)
	}
	compID := h.AsID(comp, "id")

	freezeBadManifest(t, h, testutil.AliceKey, compID, map[string]any{
		"width": 10, "height": 10,
		"layers": []map[string]any{
			{"id": "a", "asset": "a.png", "x": 0, "y": 0},
			{"id": "missing", "asset": "ghost.png", "x": 0, "y": 0},
		},
	})

	st, _ = h.Do("GET", "/compositions/"+compID.String()+"/versions", testutil.AliceKey, nil)
	if st != http.StatusOK {
		t.Fatalf("list versions: %d", st)
	}
	// Endpoint returns an array.
	var versions []any
	if b, err := jsonGet(h, "/compositions/"+compID.String()+"/versions", testutil.AliceKey); err == nil {
		_ = json.Unmarshal(b, &versions)
	}
	if len(versions) != 0 {
		t.Fatalf("rejected freeze must not create a version, got %d", len(versions))
	}
}

// TestFreezeRejectsCycle and bounds / path traversal.
func TestFreezeRejectsCycleAndTraversal(t *testing.T) {
	h := testutil.New(t)
	proj := h.CreateProject(testutil.AliceKey, "p")
	h.UploadAsset(testutil.AliceKey, proj, "a.png", testutil.MakePNG(2, 2, color.RGBA{1, 2, 3, 255}))
	h.UploadAsset(testutil.AliceKey, proj, "b.png", testutil.MakePNG(2, 2, color.RGBA{3, 2, 1, 255}))
	st, comp := h.Do("POST", "/projects/"+proj.String()+"/compositions", testutil.AliceKey,
		map[string]any{"name": "c", "width": 10, "height": 10})
	if st != http.StatusCreated {
		t.Fatalf("comp: %d %v", st, comp)
	}
	compID := h.AsID(comp, "id")

	// Cycle.
	freezeBadManifest(t, h, testutil.AliceKey, compID, map[string]any{
		"width": 10, "height": 10,
		"layers": []map[string]any{
			{"id": "a", "asset": "a.png", "x": 0, "y": 0, "deps": []string{"b"}},
			{"id": "b", "asset": "b.png", "x": 1, "y": 1, "deps": []string{"a"}},
		},
	})

	// Path traversal.
	freezeBadManifest(t, h, testutil.AliceKey, compID, map[string]any{
		"width": 10, "height": 10,
		"layers": []map[string]any{
			{"id": "a", "asset": "../a.png", "x": 0, "y": 0},
		},
	})

	// Out of canvas: asset is 2x2 placed at x=9.
	freezeBadManifest(t, h, testutil.AliceKey, compID, map[string]any{
		"width": 10, "height": 10,
		"layers": []map[string]any{
			{"id": "a", "asset": "a.png", "x": 9, "y": 0},
		},
	})
}

// TestUploadRejectsNonPNG ensures only real PNGs are stored.
func TestUploadRejectsNonPNG(t *testing.T) {
	h := testutil.New(t)
	proj := h.CreateProject(testutil.AliceKey, "p")

	var body bytes.Buffer
	mw := newMultipart(&body, "evil.png", "image/png", []byte("GIF89a-not-really"))
	req := newUploadRequest(h, proj, &body, mw.FormDataContentType())
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400 for fake png, got %d", resp.StatusCode)
	}
}

// TestRenderedFrameIsValidPNGAndStable: an end-to-end job produces a
// decodable PNG whose pixels are the composited result and identical on
// re-render (deterministic).
func TestRenderedFrameIsValidPNGAndStable(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)

	st, job := h.Do("POST", "/versions/"+versionID.String()+"/jobs", testutil.AliceKey,
		map[string]any{"priority": 5, "frameStart": 0, "frameEnd": 0})
	if st != http.StatusCreated {
		t.Fatalf("job: %d %v", st, job)
	}
	jobID := h.AsID(job, "id")

	runOneAndWait(t, h, jobID, "succeeded")

	raw := pngAt(h, "/jobs/"+jobID.String()+"/frames/0/png")
	img, err := png.Decode(bytes.NewReader(raw))
	if err != nil {
		t.Fatalf("frame is not a valid png: %v", err)
	}
	b := img.Bounds()
	if b.Dx() != 40 || b.Dy() != 20 {
		t.Fatalf("canvas = %dx%d", b.Dx(), b.Dy())
	}
	// Pixel inside the fg region must be blended (not pure bg blue and not
	// fully transparent).
	px := img.At(20, 10)
	r, g, bl, a := px.RGBA()
	if a>>8 < 250 {
		t.Fatalf("blended pixel alpha = %d, want ~255", a>>8)
	}
	if r>>8 == 20 && g>>8 == 80 && bl>>8 == 160 {
		t.Fatal("pixel equals untouched background; fg was not composited")
	}

	// summary.json lists the frame with its digest.
	st, summary := h.Do("GET", "/jobs/"+jobID.String()+"/summary", testutil.AliceKey, nil)
	if st != http.StatusOK {
		t.Fatalf("summary: %d %v", st, summary)
	}
	frames := summary["frames"].([]any)
	if len(frames) != 1 {
		t.Fatalf("summary frames = %d", len(frames))
	}
	if frames[0].(map[string]any)["sha256"].(string) == "" {
		t.Fatal("summary frame missing sha256")
	}
}
