package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"dagexec/internal/engine"
	"dagexec/internal/model"
	"dagexec/internal/server"
	"dagexec/internal/store"
	"dagexec/internal/task"
)

func newTestServer(t *testing.T) (*httptest.Server, *engine.Engine) {
	t.Helper()
	st, err := store.NewFileStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	eng := engine.New(st, task.Builtins())
	if err := eng.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = eng.Shutdown(ctx)
	})
	srv := httptest.NewServer(server.New(eng).Handler())
	t.Cleanup(srv.Close)
	return srv, eng
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestHealth(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestTaskWhitelist(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/api/tasks")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		Tasks []string `json:"tasks"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	for _, w := range []string{"const", "fail", "add"} {
		if !containsStr(out.Tasks, w) {
			t.Errorf("whitelist %v missing %q", out.Tasks, w)
		}
	}
}

func containsStr(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

func TestAPICancelBeforeSchedulingSettles(t *testing.T) {
	// Cancel an already-terminal DAG is idempotent and returns the snapshot;
	// cancel on an unknown id returns 404. (The no-new-downstream guarantee
	// for a running DAG is covered by engine tests with blocking tasks.)
	srv, _ := newTestServer(t)
	body := mustJSON(t, model.DAG{Nodes: []model.Node{
		{ID: "a", Type: "const", Params: map[string]any{"value": 1}},
	}})
	resp, err := http.Post(srv.URL+"/api/dags", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	var snap model.Snapshot
	_ = json.NewDecoder(resp.Body).Decode(&snap)
	resp.Body.Close()
	waitStatus(t, srv.URL, snap.ID)

	cr, err := http.Post(srv.URL+"/api/dags/"+snap.ID+"/cancel", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if cr.StatusCode != http.StatusOK {
		t.Fatalf("cancel terminal status=%d", cr.StatusCode)
	}
	cr.Body.Close()

	cr, err = http.Post(srv.URL+"/api/dags/unknown/cancel", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if cr.StatusCode != http.StatusNotFound {
		t.Fatalf("cancel unknown status=%d want 404", cr.StatusCode)
	}
	cr.Body.Close()
}

func waitStatus(t *testing.T, base, id string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		r, err := http.Get(base + "/api/dags/" + id)
		if err != nil {
			t.Fatal(err)
		}
		var s model.Snapshot
		_ = json.NewDecoder(r.Body).Decode(&s)
		r.Body.Close()
		if s.Status.Terminal() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("dag never reached terminal state")
}

func TestAPIDiamondSucceeds(t *testing.T) {
	srv, _ := newTestServer(t)

	body := mustJSON(t, model.DAG{Nodes: []model.Node{
		{ID: "top", Type: "const", Params: map[string]any{"value": 10}},
		{ID: "left", Type: "mul", Deps: []string{"top"},
			Params: map[string]any{"x": map[string]any{"$ref": "top"}, "y": 2}},
		{ID: "right", Type: "add", Deps: []string{"top"},
			Params: map[string]any{"x": map[string]any{"$ref": "top"}, "y": 5}},
		{ID: "bottom", Type: "sum_deps", Deps: []string{"left", "right"}},
	}})

	resp, err := http.Post(srv.URL+"/api/dags", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("submit status=%d", resp.StatusCode)
	}
	var created model.Snapshot
	if err := json.NewDecoder(resp.Body).Decode(&created); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	// Poll until terminal.
	deadline := time.Now().Add(2 * time.Second)
	var final model.Snapshot
	for time.Now().Before(deadline) {
		r, err := http.Get(srv.URL + "/api/dags/" + created.ID)
		if err != nil {
			t.Fatal(err)
		}
		if r.StatusCode != http.StatusOK {
			t.Fatalf("get status=%d", r.StatusCode)
		}
		_ = json.NewDecoder(r.Body).Decode(&final)
		r.Body.Close()
		if final.Status.Terminal() {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if final.Status != model.StatusSucceeded {
		t.Fatalf("status=%s nodes=%+v", final.Status, final.Nodes)
	}
	bot, _ := final.Nodes["bottom"].Result.(float64)
	if bot != 35 {
		t.Errorf("bottom=%v want 35", final.Nodes["bottom"].Result)
	}

	// List contains it.
	r, err := http.Get(srv.URL + "/api/dags")
	if err != nil {
		t.Fatal(err)
	}
	var list []model.Snapshot
	if err := json.NewDecoder(r.Body).Decode(&list); err != nil {
		t.Fatal(err)
	}
	r.Body.Close()
	if len(list) != 1 {
		t.Fatalf("list len=%d want 1", len(list))
	}
}

func TestAPIRejectsCycleAndMissing(t *testing.T) {
	srv, _ := newTestServer(t)

	cases := []model.DAG{
		{Nodes: []model.Node{
			{ID: "a", Type: "const", Deps: []string{"b"}, Params: map[string]any{"value": 1}},
			{ID: "b", Type: "const", Deps: []string{"a"}, Params: map[string]any{"value": 1}},
		}},
		{Nodes: []model.Node{
			{ID: "a", Type: "const", Deps: []string{"nope"}, Params: map[string]any{"value": 1}},
		}},
		{Nodes: []model.Node{{ID: "a", Type: "rm -rf /"}}},
		{}, // no nodes
	}
	for i, spec := range cases {
		resp, err := http.Post(srv.URL+"/api/dags", "application/json",
			bytes.NewReader(mustJSON(t, spec)))
		if err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != http.StatusBadRequest {
			t.Errorf("case %d: status=%d want 400", i, resp.StatusCode)
		}
		var eb map[string]string
		_ = json.NewDecoder(resp.Body).Decode(&eb)
		resp.Body.Close()
		if eb["error"] == "" {
			t.Errorf("case %d: missing error body", i)
		}
	}
}

func TestAPIRejectsMalformedAndUnknownFields(t *testing.T) {
	srv, _ := newTestServer(t)

	// Not JSON.
	resp, err := http.Post(srv.URL+"/api/dags", "application/json", bytes.NewReader([]byte("{nope")))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d want 400", resp.StatusCode)
	}
	resp.Body.Close()

	// Unknown field.
	resp, err = http.Post(srv.URL+"/api/dags", "application/json",
		bytes.NewReader([]byte(`{"nodes":[{"id":"a","type":"const","sneaky":1}]}`)))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("unknown-field status=%d want 400", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestAPIGet404(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/api/dags/doesnotexist")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status=%d want 404", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestAPIFailureSkipsDownstream(t *testing.T) {
	srv, _ := newTestServer(t)
	body := mustJSON(t, model.DAG{
		Nodes: []model.Node{
			{ID: "top", Type: "const", Params: map[string]any{"value": 1}},
			{ID: "bad", Type: "fail", Deps: []string{"top"},
				Params: map[string]any{"message": "boom"}, Retries: ptr(0)},
			{ID: "leaf", Type: "const", Deps: []string{"bad"},
				Params: map[string]any{"value": 9}},
		},
	})
	resp, err := http.Post(srv.URL+"/api/dags", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	var created model.Snapshot
	_ = json.NewDecoder(resp.Body).Decode(&created)
	resp.Body.Close()

	deadline := time.Now().Add(2 * time.Second)
	var final model.Snapshot
	for time.Now().Before(deadline) {
		r, _ := http.Get(srv.URL + "/api/dags/" + created.ID)
		_ = json.NewDecoder(r.Body).Decode(&final)
		r.Body.Close()
		if final.Status.Terminal() {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if final.Status != model.StatusFailed {
		t.Fatalf("status=%s want failed", final.Status)
	}
	if final.Nodes["bad"].Status != model.StatusFailed || final.Nodes["bad"].Error != "boom" {
		t.Errorf("bad node=%s/%q", final.Nodes["bad"].Status, final.Nodes["bad"].Error)
	}
	if final.Nodes["leaf"].Status != model.StatusSkipped {
		t.Errorf("leaf=%s want skipped", final.Nodes["leaf"].Status)
	}
}

func ptr[T any](v T) *T { return &v }
