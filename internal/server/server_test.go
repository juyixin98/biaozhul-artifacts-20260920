package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"dagscheduler/scheduler"
)

func newTestServer(t *testing.T) (*httptest.Server, func()) {
	t.Helper()
	srv := New(10*time.Millisecond, 10000)
	ts := httptest.NewServer(srv.Handler())
	return ts, ts.Close
}

func postJSON(t *testing.T, ts *httptest.Server, path string, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(ts.URL+path, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return resp.StatusCode, out
}

func getJSON(t *testing.T, ts *httptest.Server, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(ts.URL + path)
	if err != nil {
		t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func waitJobState(t *testing.T, ts *httptest.Server, id string, want string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		status, body := getJSON(t, ts, "/v1/jobs/"+id)
		if status != 200 {
			t.Fatalf("get job status=%d", status)
		}
		if body["state"] == want {
			return body
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("job %s did not reach %s within 5s", id, want)
	return nil
}

func TestHealth(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	status, body := getJSON(t, ts, "/healthz")
	if status != 200 || body["status"] != "ok" {
		t.Fatalf("health = %d %v", status, body)
	}
}

func TestSubmitAndGetDiamond(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	spec := `{
	  "name": "diamond-http",
	  "nodes": [
	    {"name": "A", "maxAttempts": 1},
	    {"name": "B", "deps": ["A"], "maxAttempts": 1},
	    {"name": "C", "deps": ["A"], "maxAttempts": 1},
	    {"name": "D", "deps": ["B", "C"], "maxAttempts": 1}
	  ]
	}`
	status, body := postJSON(t, ts, "/v1/jobs", spec)
	if status != 201 {
		t.Fatalf("submit status=%d body=%v", status, body)
	}
	id := body["id"].(string)

	final := waitJobState(t, ts, id, "SUCCEEDED")
	nodes := final["nodes"].([]any)
	states := map[string]string{}
	for _, n := range nodes {
		nm := n.(map[string]any)
		states[nm["name"].(string)] = nm["state"].(string)
	}
	for _, n := range []string{"A", "B", "C", "D"} {
		if states[n] != "SUCCEEDED" {
			t.Errorf("node %s=%s want SUCCEEDED", n, states[n])
		}
	}
}

func TestSubmitRejectsCycle(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	spec := `{"nodes":[
	  {"name":"A","deps":["B"]},
	  {"name":"B","deps":["A"]}
	]}`
	status, body := postJSON(t, ts, "/v1/jobs", spec)
	if status != 400 {
		t.Fatalf("status=%d want 400, body=%v", status, body)
	}
	if !strings.Contains(fmt.Sprint(body["message"]), "cycle") {
		t.Errorf("message=%v want cycle mention", body["message"])
	}
}

func TestFlakyRetrySucceeds(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	spec := `{"name":"flaky","nodes":[
	  {"name":"n","maxAttempts":3,"payload":{"action":"flaky","failTimes":2}}
	]}`
	status, body := postJSON(t, ts, "/v1/jobs", spec)
	if status != 201 {
		t.Fatalf("status=%d body=%v", status, body)
	}
	id := body["id"].(string)
	final := waitJobState(t, ts, id, "SUCCEEDED")
	for _, n := range final["nodes"].([]any) {
		nm := n.(map[string]any)
		if nm["name"] == "n" {
			if nm["attempts"].(float64) != 3 {
				t.Errorf("attempts=%v want 3", nm["attempts"])
			}
		}
	}

	// Events endpoint records the retries.
	status, evBody := getJSON(t, ts, "/v1/jobs/"+id+"/events")
	if status != 200 {
		t.Fatalf("events status=%d", status)
	}
	var retries, succeeded int
	for _, e := range evBody["events"].([]any) {
		switch e.(map[string]any)["type"] {
		case "NODE_RETRY_WAIT":
			retries++
		case "NODE_SUCCEEDED":
			succeeded++
		}
	}
	if retries != 2 {
		t.Errorf("retry events=%d want 2", retries)
	}
	if succeeded != 1 {
		t.Errorf("succeeded events=%d want 1", succeeded)
	}
}

func TestFailurePropagationSkipsDownstream(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	spec := `{"name":"skip-propagation","nodes":[
	  {"name":"A","maxAttempts":1},
	  {"name":"B","deps":["A"],"maxAttempts":1,"payload":{"action":"fail","message":"boom"}},
	  {"name":"C","deps":["A"],"maxAttempts":1},
	  {"name":"D","deps":["B","C"],"maxAttempts":1}
	]}`
	status, body := postJSON(t, ts, "/v1/jobs", spec)
	if status != 201 {
		t.Fatalf("status=%d", status)
	}
	id := body["id"].(string)
	final := waitJobState(t, ts, id, "FAILED")
	want := map[string]string{"A": "SUCCEEDED", "B": "FAILED", "C": "SUCCEEDED", "D": "SKIPPED"}
	for _, n := range final["nodes"].([]any) {
		nm := n.(map[string]any)
		name := nm["name"].(string)
		if nm["state"].(string) != want[name] {
			t.Errorf("node %s=%s want %s", name, nm["state"], want[name])
		}
	}
}

func TestAllEndRunsAfterFailure(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	spec := `{"name":"all-end","nodes":[
	  {"name":"A","maxAttempts":1},
	  {"name":"B","deps":["A"],"policy":"ALL_END","maxAttempts":1,"payload":{"action":"fail"}},
	  {"name":"C","deps":["B"],"policy":"ALL_END","maxAttempts":1}
	]}`
	status, body := postJSON(t, ts, "/v1/jobs", spec)
	if status != 201 {
		t.Fatalf("status=%d", status)
	}
	id := body["id"].(string)
	final := waitJobState(t, ts, id, "FAILED")
	states := map[string]string{}
	for _, n := range final["nodes"].([]any) {
		nm := n.(map[string]any)
		states[nm["name"].(string)] = nm["state"].(string)
	}
	if states["C"] != "SUCCEEDED" {
		t.Errorf("C=%s want SUCCEEDED under ALL_END", states["C"])
	}
}

func TestCancelSleepingJob(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	spec := `{"name":"cancelme","nodes":[
	  {"name":"slow","maxAttempts":1,"payload":{"action":"sleep","sleepMs":10000}},
	  {"name":"after","deps":["slow"],"maxAttempts":1}
	]}`
	status, body := postJSON(t, ts, "/v1/jobs", spec)
	if status != 201 {
		t.Fatalf("status=%d", status)
	}
	id := body["id"].(string)
	// Ensure slow started before canceling.
	waitForNodeState(t, ts, id, "slow", "RUNNING")

	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/v1/jobs/"+id+"/cancel", bytes.NewReader(nil))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("cancel: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != 202 {
		t.Fatalf("cancel status=%d want 202", resp.StatusCode)
	}

	final := waitJobState(t, ts, id, "CANCELED")
	for _, n := range final["nodes"].([]any) {
		nm := n.(map[string]any)
		if nm["name"] == "after" && nm["state"] != "SKIPPED" {
			t.Errorf("after=%s want SKIPPED", nm["state"])
		}
	}
}

func TestCancelFinishedJobConflicts(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	status, body := postJSON(t, ts, "/v1/jobs", `{"nodes":[{"name":"A"}]}`)
	if status != 201 {
		t.Fatalf("status=%d", status)
	}
	id := body["id"].(string)
	waitJobState(t, ts, id, "SUCCEEDED")
	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/v1/jobs/"+id+"/cancel", bytes.NewReader(nil))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("cancel: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 409 {
		t.Errorf("status=%d want 409", resp.StatusCode)
	}
}

func TestUnknownJob404(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	if status, _ := getJSON(t, ts, "/v1/jobs/nope"); status != 404 {
		t.Errorf("get missing job status=%d want 404", status)
	}
}

func TestListJobs(t *testing.T) {
	ts, closeFn := newTestServer(t)
	defer closeFn()
	postJSON(t, ts, "/v1/jobs", `{"name":"j1","nodes":[{"name":"A"}]}`)
	postJSON(t, ts, "/v1/jobs", `{"name":"j2","nodes":[{"name":"A"}]}`)
	status, body := getJSON(t, ts, "/v1/jobs")
	if status != 200 {
		t.Fatalf("status=%d", status)
	}
	jobs := body["jobs"].([]any)
	if len(jobs) != 2 {
		t.Fatalf("len(jobs)=%d want 2", len(jobs))
	}
}

// guard against accidental drift of public event/state string values.
func TestEventStringStability(t *testing.T) {
	if scheduler.EventNodeSkipped != "NODE_SKIPPED" {
		t.Fatal("event string drifted")
	}
	if scheduler.RequireAllEnd != "ALL_END" {
		t.Fatal("policy string drifted")
	}
}

func waitForNodeState(t *testing.T, ts *httptest.Server, id, node, want string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		_, body := getJSON(t, ts, "/v1/jobs/"+id)
		for _, n := range body["nodes"].([]any) {
			nm := n.(map[string]any)
			if nm["name"] == node && nm["state"] == want {
				return
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("node %s never reached %s", node, want)
}
