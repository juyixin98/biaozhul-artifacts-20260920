package server_test

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/example/merklekv/internal/hlc"
	"github.com/example/merklekv/internal/merkle"
	"github.com/example/merklekv/internal/server"
	"github.com/example/merklekv/internal/store"
)

func newTestServer(t *testing.T, p merkle.Params) (*httptest.Server, *store.Store) {
	t.Helper()
	st := store.New(store.Config{ReplicaID: "r1"})
	srv := httptest.NewServer(server.New(st, p, nil).Handler())
	t.Cleanup(srv.Close)
	return srv, st
}

func postJSON(t *testing.T, url string, body any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(body)
	resp, err := http.Post(url, "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var m map[string]any
	if len(data) > 0 {
		_ = json.Unmarshal(data, &m)
	}
	return resp.StatusCode, m
}

func TestSnapshotNodeEntriesRoundTrip(t *testing.T) {
	params, _ := merkle.NewParams(4, 2) // 16 buckets
	srv, st := newTestServer(t, params)
	st.Seed([]store.Entry{
		{Key: "alpha", Value: []byte("1"), Ver: hlc.Timestamp{Wall: 1}, Origin: "r1"},
		{Key: "beta", Value: []byte("2"), Ver: hlc.Timestamp{Wall: 2}, Origin: "r1"},
	})

	code, snap := postJSON(t, srv.URL+"/v1/snapshots", nil)
	if code != 200 {
		t.Fatalf("snapshot status %d", code)
	}
	id := snap["snapshot_id"].(string)
	root := snap["root_hash"].(string)
	if root == "" {
		t.Fatal("empty root hash")
	}

	// Root node: level 0, fanout children.
	code, m := postJSON(t, srv.URL+"/v1/nodes", map[string]any{"snapshot_id": id, "paths": []string{""}})
	if code != 200 {
		t.Fatalf("nodes status %d: %v", code, m)
	}
	nodes := m["nodes"].([]any)
	rootNode := nodes[0].(map[string]any)
	if rootNode["level"].(float64) != 0 || len(rootNode["children"].([]any)) != 4 {
		t.Fatalf("bad root node: %v", rootNode)
	}

	// Unknown snapshot -> 404.
	code, m = postJSON(t, srv.URL+"/v1/nodes", map[string]any{"snapshot_id": "deadbeef", "paths": []string{""}})
	if code != http.StatusNotFound || m["error"] != "snapshot_not_found" {
		t.Fatalf("want 404 snapshot_not_found, got %d %v", code, m)
	}

	// Fetch entries of the bucket holding "alpha".
	bucket := params.BucketIndex("alpha")
	code, m = postJSON(t, srv.URL+"/v1/entries", map[string]any{"snapshot_id": id, "buckets": []int{bucket}})
	if code != 200 {
		t.Fatalf("entries status %d: %v", code, m)
	}
	buckets := m["buckets"].([]any)
	ents := buckets[0].(map[string]any)["entries"].([]any)
	found := false
	for _, e := range ents {
		if e.(map[string]any)["key"] == "alpha" {
			found = true
		}
	}
	if !found {
		t.Fatal("alpha not found in its bucket")
	}

	// Out-of-range bucket -> 400.
	code, _ = postJSON(t, srv.URL+"/v1/entries", map[string]any{"snapshot_id": id, "buckets": []int{999}})
	if code != http.StatusBadRequest {
		t.Fatalf("want 400 for bad bucket, got %d", code)
	}
}

func TestApplyEpochGuard(t *testing.T) {
	params, _ := merkle.NewParams(4, 1)
	srv, st := newTestServer(t, params)
	st.Put("k", []byte("v1"))
	_, snap := postJSON(t, srv.URL+"/v1/snapshots", nil)
	epoch := int64(snap["epoch"].(float64))

	// Mutate so the pinned epoch goes stale.
	st.Put("other", []byte("v2"))

	entry := map[string]any{
		"key": "remote", "value": []byte("z"), "origin": "r2",
		"ver": map[string]any{"wall": 99999, "log": 0},
	}
	code, m := postJSON(t, srv.URL+"/v1/apply",
		map[string]any{"expected_epoch": epoch, "entries": []any{entry}})
	if code != http.StatusConflict || m["error"] != "epoch_moved" {
		t.Fatalf("want 409 epoch_moved, got %d %v", code, m)
	}
	if _, ok := st.Get("remote"); ok {
		t.Fatal("guarded-out apply must not write")
	}

	// Current epoch applies cleanly.
	cur := st.Epoch()
	code, m = postJSON(t, srv.URL+"/v1/apply",
		map[string]any{"expected_epoch": cur, "entries": []any{entry}})
	if code != 200 || m["applied"].(float64) != 1 {
		t.Fatalf("fresh-epoch apply failed: %d %v", code, m)
	}
	if got, ok := st.Get("remote"); !ok || string(got.Value) != "z" {
		t.Fatal("applied value missing")
	}
}

func TestStaleOlderVersionIgnored(t *testing.T) {
	params, _ := merkle.NewParams(4, 1)
	srv, st := newTestServer(t, params)
	st.Seed([]store.Entry{{Key: "k", Value: []byte("new"), Ver: hlc.Timestamp{Wall: 100}, Origin: "r1"}})
	old := map[string]any{
		"key": "k", "value": []byte("old"), "origin": "r2",
		"ver": map[string]any{"wall": 1, "log": 0},
	}
	code, m := postJSON(t, srv.URL+"/v1/apply",
		map[string]any{"expected_epoch": -1, "entries": []any{old}})
	if code != 200 || m["unchanged"].(float64) != 1 || m["applied"].(float64) != 0 {
		t.Fatalf("older version must be ignored: %d %v", code, m)
	}
	if got, _ := st.Get("k"); string(got.Value) != "new" {
		t.Fatal("older version overwrote newer one")
	}
}

func TestBadJSONRejected(t *testing.T) {
	params, _ := merkle.NewParams(4, 1)
	srv, _ := newTestServer(t, params)
	resp, err := http.Post(srv.URL+"/v1/apply", "application/json", bytes.NewBufferString("{not json"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400 for malformed JSON, got %d", resp.StatusCode)
	}
}
