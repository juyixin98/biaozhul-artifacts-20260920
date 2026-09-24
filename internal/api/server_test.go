package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"deadlockcheck/internal/core"
	"deadlockcheck/internal/evidence"
	"deadlockcheck/internal/store"
	"deadlockcheck/internal/testutil"
)

func newTestServer(t *testing.T) (*httptest.Server, context.Context) {
	t.Helper()
	ctx := context.Background()
	dsn := testutil.IsolatedDSN(t)
	pool := testutil.NewPool(t, dsn)
	if err := pool.Ping(ctx); err != nil {
		t.Skipf("postgres unreachable: %v", err)
	}
	if err := (&store.Store{Pool: pool}).Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	key, _ := evidence.GenerateKey()
	raw, _ := evidence.ParseKey(key)
	svc := core.NewService(pool, evidence.NewSigner(raw), 1.0)
	srv := httptest.NewServer(NewServer(svc).Router())
	t.Cleanup(srv.Close)
	return srv, ctx
}

func postJSON(t *testing.T, url string, body any) (int, map[string]any) {
	t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(url, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func getJSON(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestHTTPCycleReturns409WithEvidence(t *testing.T) {
	srv, _ := newTestServer(t)

	if code, _ := postJSON(t, srv.URL+"/api/v1/resources", map[string]any{
		"resources": []map[string]string{
			{"kind": "tool", "name": "X"},
			{"kind": "tool", "name": "Y"},
		},
	}); code != http.StatusCreated {
		t.Fatalf("register resources status=%d", code)
	}

	code, a := postJSON(t, srv.URL+"/api/v1/tasks", map[string]any{
		"label": "A", "priority": 100, "timeout_ms": 60000,
		"resources": []map[string]string{{"kind": "tool", "name": "X"}},
	})
	if code != http.StatusCreated {
		t.Fatalf("create A status=%d body=%v", code, a)
	}
	_, b := postJSON(t, srv.URL+"/api/v1/tasks", map[string]any{
		"label": "B", "priority": 100, "timeout_ms": 60000,
		"resources": []map[string]string{{"kind": "tool", "name": "Y"}},
	})
	aid := int64(a["task_id"].(float64))
	bid := int64(b["task_id"].(float64))

	// A -> Y held by B: 202 waiting with wait_reasons.
	code, ar := postJSON(t, srv.URL+"/api/v1/tasks/"+itoa(aid)+"/requests",
		map[string]any{"resources": []map[string]string{{"kind": "tool", "name": "Y"}}})
	if code != http.StatusAccepted {
		t.Fatalf("A extra Y should be 202, got %d %v", code, ar)
	}
	reasons := ar["wait_reasons"].([]any)
	if len(reasons) != 1 {
		t.Fatalf("expected one wait reason, got %v", reasons)
	}

	// B -> X closes the cycle: 409 cycle_detected with cycle path.
	code, cy := postJSON(t, srv.URL+"/api/v1/tasks/"+itoa(bid)+"/requests",
		map[string]any{"resources": []map[string]string{{"kind": "tool", "name": "X"}}})
	if code != http.StatusConflict {
		t.Fatalf("cycle close must be 409, got %d %v", code, cy)
	}
	if cy["error"] != "cycle_detected" {
		t.Fatalf("error kind mismatch: %v", cy)
	}
	if cy["details"].(map[string]any)["cycle"] == nil {
		t.Fatalf("cycle path must be returned: %v", cy["details"])
	}

	// Holds endpoint shows who holds what — the resource-holding evidence.
	code, _ = getJSON(t, srv.URL+"/api/v1/holds")
	if code != 200 {
		t.Fatalf("holds status=%d", code)
	}
}

func TestHTTPUnknownResource404AndBadJSON400(t *testing.T) {
	srv, _ := newTestServer(t)
	code, body := postJSON(t, srv.URL+"/api/v1/tasks", map[string]any{
		"label": "ghost", "timeout_ms": 1000,
		"resources": []map[string]string{{"kind": "tool", "name": "nope"}},
	})
	if code != http.StatusNotFound || body["error"] != "not_found" {
		t.Fatalf("expected 404 not_found, got %d %v", code, body)
	}

	resp, err := http.Post(srv.URL+"/api/v1/resources", "application/json",
		bytes.NewReader([]byte("{not json")))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("malformed JSON must be 400, got %d", resp.StatusCode)
	}
}

func TestHTTPUncertainFenceEndToEnd(t *testing.T) {
	srv, _ := newTestServer(t)
	postJSON(t, srv.URL+"/api/v1/resources", map[string]any{
		"resources": []map[string]string{{"kind": "tool", "name": "drill"}},
	})
	_, a := postJSON(t, srv.URL+"/api/v1/tasks", map[string]any{
		"label": "slow", "timeout_ms": 150,
		"resources": []map[string]string{{"kind": "tool", "name": "drill"}},
	})
	_, b := postJSON(t, srv.URL+"/api/v1/tasks", map[string]any{
		"label": "next", "timeout_ms": 60000,
		"resources": []map[string]string{{"kind": "tool", "name": "drill"}},
	})
	bid := int64(b["task_id"].(float64))

	// Wait for A's deadline, then force the sweep via the admin endpoint.
	waitMS(220)
	code, sweep := postJSON(t, srv.URL+"/api/v1/admin/sweep", map[string]any{})
	if code != 200 {
		t.Fatalf("sweep failed status=%d", code)
	}
	ids := sweep["transitioned_to_uncertain"].([]any)
	if len(ids) != 1 {
		t.Fatalf("exactly A should become uncertain, got %v", ids)
	}
	code, bt := getJSON(t, srv.URL+"/api/v1/tasks/"+itoa(bid))
	if code != 200 || bt["state"] != "waiting" {
		t.Fatalf("B must remain waiting after A timed out, got %d state=%v", code, bt["state"])
	}

	// Evidence endpoint lists the timed_out signed record.
	code, _ = getJSON(t, srv.URL+"/api/v1/evidence?task_id="+itoa(int64(a["task_id"].(float64))))
	if code != 200 {
		t.Fatalf("evidence status=%d", code)
	}
}

func waitMS(ms int) {
	<-time.After(time.Duration(ms) * time.Millisecond)
}

// itoa avoids importing strconv in this tiny helper scope.
func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
