package server_test

import (
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/example/merklekv/internal/hlc"
	"github.com/example/merklekv/internal/merkle"
	"github.com/example/merklekv/internal/store"
)

func do(t *testing.T, method, url string, body string) (int, string, http.Header) {
	t.Helper()
	var rdr io.Reader
	if body != "" {
		rdr = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(data), resp.Header
}

func TestKVPutGetDeleteLifecycle(t *testing.T) {
	params, _ := merkle.NewParams(4, 1)
	srv, _ := newTestServer(t, params)

	code, body, _ := do(t, http.MethodPut, srv.URL+"/v1/kv/color", "blue")
	if code != 200 {
		t.Fatalf("put = %d", code)
	}
	if !strings.Contains(body, `"origin":"r1"`) || !strings.Contains(body, `"ver"`) {
		t.Fatalf("put body should carry the versioned entry, got %s", body)
	}

	code, body, _ = do(t, http.MethodGet, srv.URL+"/v1/kv/color", "")
	if code != 200 || body != "blue" {
		t.Fatalf("get = %d %q", code, body)
	}

	// First delete of an existing key returns 200.
	code, _, _ = do(t, http.MethodDelete, srv.URL+"/v1/kv/color", "")
	if code != 200 {
		t.Fatalf("delete existing = %d, want 200", code)
	}
	if code, _, _ = do(t, http.MethodGet, srv.URL+"/v1/kv/color", ""); code != 404 {
		t.Fatalf("get after delete = %d, want 404", code)
	}

	// Deleting a never-seen key records a tombstone -> 201.
	code, _, _ = do(t, http.MethodDelete, srv.URL+"/v1/kv/ghost", "")
	if code != 201 {
		t.Fatalf("delete unseen = %d, want 201", code)
	}
}

func TestHealthConfigDebugTreeAndSeed(t *testing.T) {
	params, _ := merkle.NewParams(4, 2) // 16 buckets, 3 levels -> 1+4+16 nodes
	srv, st := newTestServer(t, params)

	if code, body, _ := do(t, http.MethodGet, srv.URL+"/healthz", ""); code != 200 ||
		!strings.Contains(body, `"status":"ok"`) {
		t.Fatalf("health = %d %s", code, body)
	}
	if code, body, _ := do(t, http.MethodGet, srv.URL+"/v1/config", ""); code != 200 ||
		!strings.Contains(body, `"buckets":16`) {
		t.Fatalf("config = %d %s", code, body)
	}

	st.Seed([]store.Entry{{Key: "k", Value: []byte("v"), Ver: hlc.Timestamp{Wall: 1}, Origin: "r1"}})

	code, body := 0, ""
	code, body, _ = do(t, http.MethodGet, srv.URL+"/v1/debug/tree", "")
	if code != 200 {
		t.Fatalf("debug tree = %d %s", code, body)
	}
	// Root + 4 level-1 + 16 leaves = 21 node entries.
	if n := strings.Count(body, `"level":`); n != 21 {
		t.Fatalf("debug tree returned %d nodes, want 21", n)
	}

	code, body, _ = do(t, http.MethodGet, srv.URL+"/v1/debug/state", "")
	if code != 200 || !strings.Contains(body, `"replica_id":"r1"`) {
		t.Fatalf("debug state = %d %s", code, body)
	}
}

func TestSnapshotRelease(t *testing.T) {
	params, _ := merkle.NewParams(4, 1)
	srv, _ := newTestServer(t, params)

	_, snap := postJSON(t, srv.URL+"/v1/snapshots", nil)
	id := snap["snapshot_id"].(string)

	code, _ := postJSON(t, srv.URL+"/v1/snapshots/"+id+"/release", nil)
	if code != 200 {
		t.Fatalf("release = %d", code)
	}
	code, m := postJSON(t, srv.URL+"/v1/nodes", map[string]any{"snapshot_id": id, "paths": []string{""}})
	if code != 404 || m["error"] != "snapshot_not_found" {
		t.Fatalf("released snapshot should 404, got %d", code)
	}
}

func TestSyncEndpointRequiresPeer(t *testing.T) {
	params, _ := merkle.NewParams(4, 1)
	srv, _ := newTestServer(t, params)
	code, m := postJSON(t, srv.URL+"/v1/sync", map[string]any{})
	if code != 400 || m["error"] != "missing_peer" {
		t.Fatalf("want 400 missing_peer, got %d %v", code, m)
	}
}
