package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/example/snapprune/internal/store"
)

func newTestServer(t *testing.T) (*httptest.Server, *store.Store) {
	t.Helper()
	st, err := store.Open(t.TempDir(), store.Options{
		KeepSnapshots: 3,
		Logger:        func(string, ...any) {},
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(NewServer(st).Handler())
	t.Cleanup(func() { srv.Close(); st.Close() })
	return srv, st
}

func post(t *testing.T, url string, body any, wantCode int) map[string]any {
	t.Helper()
	data, _ := json.Marshal(body)
	resp, err := http.Post(url, "application/json", bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if resp.StatusCode != wantCode {
		t.Fatalf("POST %s: status = %d, want %d (body %v)", url, resp.StatusCode, wantCode, out)
	}
	return out
}

func get(t *testing.T, url string, wantCode int) map[string]any {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if resp.StatusCode != wantCode {
		t.Fatalf("GET %s: status = %d, want %d (body %v)", url, resp.StatusCode, wantCode, out)
	}
	return out
}

// TestHTTPFlow exercises the full API: blocks in, snapshot, historical
// query, prune, and the explicit 410 for pruned heights.
func TestHTTPFlow(t *testing.T) {
	srv, st := newTestServer(t)

	for i := 0; i < 20; i++ {
		out := post(t, srv.URL+"/v1/blocks", map[string]any{
			"changes": map[string]int64{"alice": 10, "bob": -3},
		}, http.StatusCreated)
		if got := out["height"].(float64); got != float64(i+1) {
			t.Fatalf("height = %v, want %d", got, i+1)
		}
	}

	head := get(t, srv.URL+"/v1/head", http.StatusOK)
	if head["height"].(float64) != 20 {
		t.Fatalf("head = %v", head)
	}

	post(t, srv.URL+"/v1/snapshots/build", map[string]any{"height": 10}, http.StatusCreated)

	state := get(t, srv.URL+"/v1/state/10", http.StatusOK)
	if state["accounts"].(float64) != 2 || state["total_balance"].(float64) != 70 {
		t.Fatalf("state at 10 = %v", state)
	}
	if state["base_snapshot"].(float64) != 10 {
		t.Fatalf("base_snapshot = %v, want 10", state["base_snapshot"])
	}

	acct := get(t, srv.URL+"/v1/state/15/accounts/alice", http.StatusOK)
	if acct["balance"].(float64) != 150 {
		t.Fatalf("alice@15 = %v, want 150", acct)
	}

	// Future height -> 400.
	get(t, srv.URL+"/v1/state/99", http.StatusBadRequest)

	// Build more snapshots to trigger pruning (keep=3).
	for _, h := range []uint64{12, 14, 16} {
		if _, err := st.BuildSnapshot(context.Background(), h); err != nil {
			t.Fatal(err)
		}
	}
	snaps := get(t, srv.URL+"/v1/snapshots", http.StatusOK)
	list := snaps["snapshots"].([]any)
	if len(list) != 3 {
		t.Fatalf("snapshots = %v, want 3 entries", list)
	}

	// Height 5 is pruned -> explicit 410.
	out := get(t, srv.URL+"/v1/state/5", http.StatusGone)
	if out["error"] == nil {
		t.Fatalf("expected error body for pruned height, got %v", out)
	}

	// Reader timeout -> explicit 408 (timeout_ms=0 = expire immediately).
	out = get(t, srv.URL+"/v1/state/20?timeout_ms=0", http.StatusRequestTimeout)
	if out["error"] == nil {
		t.Fatalf("expected timeout error body, got %v", out)
	}
	fmt.Println("http flow ok")
}
