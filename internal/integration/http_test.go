package integration_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"image/color"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"vfxqueue/internal/apiserver"
	"vfxqueue/internal/compositor"
	"vfxqueue/internal/testsupport"
	"vfxqueue/internal/worker"

	"github.com/google/uuid"
)

// apiH bundles a test server + token for a user.
type apiH struct {
	h     *testsupport.Harness
	srv   *httptest.Server
	admin string
	alice string
	bob   string
}

func newAPI(t *testing.T) *apiH {
	h := testsupport.New(t)
	srv := apiserver.New(h.Pool, h.Q, h.Config())
	ts := httptest.NewServer(srv.Router())
	t.Cleanup(ts.Close)

	a := &apiH{h: h, srv: ts}
	a.admin = h.CreateUser("admin").ApiToken
	a.alice = h.CreateUser("member").ApiToken
	a.bob = h.CreateUser("member").ApiToken
	return a
}

func (a *apiH) do(method, path, token string, body any) (int, map[string]any) {
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, a.srv.URL+path, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		a.h.T.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func (a *apiH) mustID(m map[string]any, key string) uuid.UUID {
	s, ok := m[key].(string)
	if !ok {
		a.h.T.Fatalf("missing %s in %v", key, m)
	}
	id, err := uuid.Parse(s)
	if err != nil {
		a.h.T.Fatalf("bad uuid in %s: %v", key, m)
	}
	return id
}

func TestAPI_AuthRequired(t *testing.T) {
	a := newAPI(t)
	sc, _ := a.do("GET", "/v1/me", "", nil)
	if sc != http.StatusUnauthorized {
		t.Fatalf("status=%d", sc)
	}
	sc, _ = a.do("GET", "/v1/me", "garbage", nil)
	if sc != http.StatusUnauthorized {
		t.Fatalf("bad token status=%d", sc)
	}
}

func TestAPI_MemberCannotAccessOthersProject(t *testing.T) {
	a := newAPI(t)
	sc, body := a.do("POST", "/v1/projects", a.alice, map[string]any{"name": "Alice's"})
	if sc != 201 {
		t.Fatalf("create project status=%d body=%v", sc, body)
	}
	pid := a.mustID(body, "id")

	// Bob (non-member) is forbidden on every project-scoped route.
	for _, path := range []string{
		"/", "/assets", "/compositions", "/tasks", "/members",
	} {
		sc, _ := a.do("GET", "/v1/projects/"+pid.String()+path, a.bob, nil)
		if sc != http.StatusForbidden {
			t.Fatalf("GET %s as bob status=%d, want 403", path, sc)
		}
	}

	// Admin can access anything.
	sc, _ = a.do("GET", "/v1/projects/"+pid.String(), a.admin, nil)
	if sc != http.StatusOK {
		t.Fatalf("admin access status=%d", sc)
	}
}

func TestAPI_AdminCanCancelAnyTask(t *testing.T) {
	a := newAPI(t)
	h := a.h

	// Create alice's project + asset + composition + task through the DB for
	// brevity, then cancel via the admin token over HTTP.
	alice, err := h.Q.GetUserByAPIToken(h.Ctx, a.alice)
	if err != nil {
		t.Fatal(err)
	}
	p := h.CreateProject(alice)
	asset := h.CreateAsset(p, alice, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{1, 2, 3, 255}))
	comp, ver := h.FreezeVersion(p, alice, (&compositor.Spec{
		CanvasWidth: 4, CanvasHeight: 4, FrameCount: 2,
		Layers: []compositor.Layer{{ID: "l", AssetID: asset.ID.String()}},
	}))
	task := h.Enqueue(p, comp, ver, 0, 1, 5, alice)

	// Bob (not a member) cannot cancel it.
	sc, _ := a.do("POST", "/v1/tasks/"+task.ID.String()+"/cancel", a.bob, nil)
	if sc != http.StatusForbidden {
		t.Fatalf("bob cancel status=%d, want 403", sc)
	}
	// Alice can cancel her own. (Reset state first: admin also could, but use
	// admin explicitly to prove cross-user authority.)
	sc, body := a.do("POST", "/v1/tasks/"+task.ID.String()+"/cancel", a.admin, nil)
	if sc != http.StatusOK {
		t.Fatalf("admin cancel status=%d body=%v", sc, body)
	}
	if body["status"] != "cancelled" {
		t.Fatalf("status=%v", body["status"])
	}
	// Second cancel is idempotent/conflict-safe.
	sc, _ = a.do("POST", "/v1/tasks/"+task.ID.String()+"/cancel", a.admin, nil)
	if sc != http.StatusOK && sc != http.StatusConflict {
		t.Fatalf("second cancel status=%d", sc)
	}
}

func TestAPI_RejectsNonPNGUpload(t *testing.T) {
	a := newAPI(t)
	sc, body := a.do("POST", "/v1/projects", a.alice, map[string]any{"name": "p"})
	if sc != 201 {
		t.Fatal(body)
	}
	pid := a.mustID(body, "id")

	// Multipart upload of plain text -> 415.
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	part, err := mw.CreateFormFile("file", "evil.png")
	if err != nil {
		t.Fatal(err)
	}
	part.Write([]byte("not a png at all"))
	mw.Close()

	req, _ := http.NewRequest("POST", a.srv.URL+"/v1/projects/"+pid.String()+"/assets", &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	req.Header.Set("Authorization", "Bearer "+a.alice)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnsupportedMediaType {
		t.Fatalf("upload status=%d, want 415", resp.StatusCode)
	}
}

func TestAPI_RejectsCompositionWithMissingResource(t *testing.T) {
	a := newAPI(t)
	sc, body := a.do("POST", "/v1/projects", a.alice, map[string]any{"name": "p"})
	if sc != 201 {
		t.Fatal(body)
	}
	pid := a.mustID(body, "id")

	bogus := uuid.New().String()
	sc, body = a.do("POST", "/v1/projects/"+pid.String()+"/compositions", a.alice, map[string]any{
		"name": "c", "canvas_width": 4, "canvas_height": 4, "frame_count": 1,
		"layers": []map[string]any{{"id": "l", "asset_id": bogus, "x": 0, "y": 0}},
	})
	if sc != http.StatusBadRequest {
		t.Fatalf("status=%d body=%v, want 400 missing resource", sc, body)
	}
}

func TestAPI_RejectsCycleAndOutOfRange(t *testing.T) {
	a := newAPI(t)
	sc, body := a.do("POST", "/v1/projects", a.alice, map[string]any{"name": "p"})
	if sc != 201 {
		t.Fatal(body)
	}
	pid := a.mustID(body, "id")

	// Need two real assets.
	upload := func(c color.NRGBA) uuid.UUID {
		data := testsupport.SolidPNG(4, 4, c)
		var buf bytes.Buffer
		mw := multipart.NewWriter(&buf)
		part, _ := mw.CreateFormFile("file", "x.png")
		part.Write(data)
		mw.Close()
		req, _ := http.NewRequest("POST", a.srv.URL+"/v1/projects/"+pid.String()+"/assets", &buf)
		req.Header.Set("Content-Type", mw.FormDataContentType())
		req.Header.Set("Authorization", "Bearer "+a.alice)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		raw, _ := io.ReadAll(resp.Body)
		json.Unmarshal(raw, &out)
		if resp.StatusCode != 201 && resp.StatusCode != 200 {
			t.Fatalf("upload status=%d %s", resp.StatusCode, raw)
		}
		return a.mustID(out, "id")
	}
	x := upload(color.NRGBA{1, 2, 3, 255})
	y := upload(color.NRGBA{4, 5, 6, 255})

	// Cyclic deps.
	sc, body = a.do("POST", "/v1/projects/"+pid.String()+"/compositions", a.alice, map[string]any{
		"name": "cyc", "canvas_width": 4, "canvas_height": 4, "frame_count": 1,
		"layers": []map[string]any{
			{"id": "a", "asset_id": x.String(), "deps": []string{"b"}},
			{"id": "b", "asset_id": y.String(), "deps": []string{"a"}},
		},
	})
	if sc != 400 {
		t.Fatalf("cycle status=%d body=%v", sc, body)
	}

	// Path traversal-like out-of-bounds origin.
	sc, _ = a.do("POST", "/v1/projects/"+pid.String()+"/compositions", a.alice, map[string]any{
		"name": "oob", "canvas_width": 4, "canvas_height": 4, "frame_count": 1,
		"layers": []map[string]any{{"id": "a", "asset_id": x.String(), "x": 9999, "y": 0}},
	})
	if sc != 400 {
		t.Fatalf("oob status=%d", sc)
	}
}

// TestAPI_EndToEndRenderAndFetch covers project -> upload -> composition ->
// task -> wait -> fetch PNG with checksum, entirely over HTTP.
func TestAPI_EndToEndRenderAndFetch(t *testing.T) {
	a := newAPI(t)
	h := a.h
	cfg := h.Config()
	ctx, cancel := context.WithCancel(h.Ctx)
	w := worker.New(h.Pool, h.Q, cfg)
	go w.Run(ctx)
	defer cancel()

	sc, body := a.do("POST", "/v1/projects", a.alice, map[string]any{"name": "e2e"})
	if sc != 201 {
		t.Fatal(body)
	}
	pid := a.mustID(body, "id")

	// Upload.
	data := testsupport.SolidPNG(4, 4, color.NRGBA{10, 20, 30, 255})
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	part, _ := mw.CreateFormFile("file", "x.png")
	part.Write(data)
	mw.Close()
	req, _ := http.NewRequest("POST", a.srv.URL+"/v1/projects/"+pid.String()+"/assets", &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	req.Header.Set("Authorization", "Bearer "+a.alice)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var assetBody map[string]any
	raw, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	json.Unmarshal(raw, &assetBody)
	if resp.StatusCode != 201 {
		t.Fatalf("upload %d %s", resp.StatusCode, raw)
	}
	assetID := assetBody["id"].(string)

	// Composition.
	sc, cbody := a.do("POST", "/v1/projects/"+pid.String()+"/compositions", a.alice, map[string]any{
		"name": "c", "canvas_width": 4, "canvas_height": 4, "frame_count": 2,
		"layers": []map[string]any{{"id": "l", "asset_id": assetID}},
	})
	if sc != 201 {
		t.Fatalf("composition %d %v", sc, cbody)
	}
	compID := a.mustID(cbody, "composition_id")

	// Task frames 0..1, priority 5.
	sc, tbody := a.do("POST", "/v1/projects/"+pid.String()+"/tasks", a.alice, map[string]any{
		"composition_id": compID.String(), "frame_start": 0, "frame_end": 1, "priority": 5,
	})
	if sc != 201 {
		t.Fatalf("task %d %v", sc, tbody)
	}
	taskID := a.mustID(tbody, "id")

	// Poll task until succeeded.
	deadline := time.Now().Add(15 * time.Second)
	var final map[string]any
	for time.Now().Before(deadline) {
		sc, final = a.do("GET", "/v1/tasks/"+taskID.String(), a.alice, nil)
		if final["status"] == "succeeded" {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if final["status"] != "succeeded" {
		t.Fatalf("task did not succeed: %v", final)
	}

	// Fetch a frame PNG and verify it is a real PNG.
	req, _ = http.NewRequest("GET",
		fmt.Sprintf("%s/v1/tasks/%s/frames/0/png", a.srv.URL, taskID), nil)
	req.Header.Set("Authorization", "Bearer "+a.alice)
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("png status=%d", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "image/png" {
		t.Fatalf("content-type=%s", ct)
	}
	pngBytes, _ := io.ReadAll(resp.Body)
	if _, err := compositor.VerifyOutputBytes(pngBytes); err != nil {
		t.Fatalf("fetched png invalid: %v", err)
	}
}
