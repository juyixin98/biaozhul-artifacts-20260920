package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"dagscheduler/scheduler"
	"dagscheduler/server"
)

func newTestServer(t *testing.T) (*httptest.Server, *scheduler.Engine) {
	t.Helper()
	eng := scheduler.New(scheduler.NewDefaultRegistry())
	srv := httptest.NewServer(server.New(eng).Handler())
	t.Cleanup(func() {
		srv.Close()
		eng.Close()
	})
	return srv, eng
}

func postJSON(t *testing.T, url string, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(url, "application/json", bytes.NewBufferString(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return resp.StatusCode, out
}

func getJSON(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return resp.StatusCode, out
}

func TestHealth(t *testing.T) {
	srv, _ := newTestServer(t)
	status, body := getJSON(t, srv.URL+"/health")
	if status != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("status=%d body=%v", status, body)
	}
}

func TestCreateGetListRun(t *testing.T) {
	srv, _ := newTestServer(t)
	body := `{
		"id": "demo",
		"nodes": [
			{"id": "a", "task_type": "noop"},
			{"id": "b", "task_type": "noop", "depends_on": ["a"]}
		]
	}`
	status, out := postJSON(t, srv.URL+"/runs", body)
	if status != http.StatusCreated {
		t.Fatalf("create status=%d body=%v", status, out)
	}
	id, _ := out["id"].(string)
	if id == "" {
		t.Fatal("missing run id")
	}

	// Submit returns as soon as the run is launched; poll for completion.
	deadline := time.Now().Add(2 * time.Second)
	var got map[string]any
	for {
		var status int
		status, got = getJSON(t, srv.URL+"/runs/"+id)
		if status != http.StatusOK {
			t.Fatalf("get status=%d", status)
		}
		if got["status"] == "succeeded" || got["status"] == "failed" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("run did not finish, status=%v", got["status"])
		}
		time.Sleep(time.Millisecond)
	}
	if got["id"] != id {
		t.Errorf("id mismatch")
	}
	nodes := got["nodes"].(map[string]any)
	if nodes["b"].(map[string]any)["status"] != "succeeded" {
		t.Errorf("b not succeeded")
	}

	resp, err := http.Get(srv.URL + "/runs")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("list status=%d", resp.StatusCode)
	}
	var list []map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		t.Fatalf("decode list: %v", err)
	}
	if len(list) != 1 || list[0]["id"] != id {
		t.Fatalf("list = %v", list)
	}

	status, events := getJSON(t, srv.URL+"/runs/"+id+"/events")
	if status != http.StatusOK {
		t.Fatalf("events status=%d", status)
	}
	evs := events["events"].([]any)
	if len(evs) == 0 {
		t.Fatal("no events")
	}
}

func TestCreateRejectsCycle(t *testing.T) {
	srv, _ := newTestServer(t)
	body := `{"nodes":[
		{"id":"a","task_type":"noop","depends_on":["b"]},
		{"id":"b","task_type":"noop","depends_on":["a"]}
	]}`
	status, out := postJSON(t, srv.URL+"/runs", body)
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d want 400 body=%v", status, out)
	}
	if !strings.Contains(out["error"].(string), "cycle") {
		t.Errorf("error should mention cycle: %v", out["error"])
	}
}

func TestCreateRejectsUnknownTask(t *testing.T) {
	srv, _ := newTestServer(t)
	status, out := postJSON(t, srv.URL+"/runs",
		`{"nodes":[{"id":"a","task_type":"ghost"}]}`)
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d body=%v", status, out)
	}
}

func TestUnknownRunReturns404(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/runs/nope")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestMalformedJSONIs400(t *testing.T) {
	srv, _ := newTestServer(t)
	status, _ := postJSON(t, srv.URL+"/runs", `{not json`)
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d", status)
	}
}

func TestCancelRunOverHTTP(t *testing.T) {
	srv, eng := newTestServer(t)
	// Long sleep so the run stays in-flight until canceled.
	body := `{"nodes":[{"id":"slow","task_type":"sleep","params":{"duration":"30s"}}]}`
	status, out := postJSON(t, srv.URL+"/runs", body)
	if status != http.StatusCreated {
		t.Fatalf("create: %d %v", status, out)
	}
	id := out["id"].(string)

	// Wait until running.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	for {
		snap, err := eng.Get(id)
		if err != nil {
			t.Fatal(err)
		}
		if snap.Nodes["slow"].Status == scheduler.StatusRunning {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatal("node never started")
		case <-time.After(time.Millisecond):
		}
	}

	req, _ := http.NewRequest(http.MethodPost,
		srv.URL+"/runs/"+id+"/cancel", strings.NewReader(`{"reason":"api test"}`))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("cancel status=%d", resp.StatusCode)
	}

	final, err := eng.Wait(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if final.Status != scheduler.RunCanceled {
		t.Fatalf("final = %s", final.Status)
	}
	if final.Nodes["slow"].Status != scheduler.StatusCanceled {
		t.Fatalf("node = %s", final.Nodes["slow"].Status)
	}
}
