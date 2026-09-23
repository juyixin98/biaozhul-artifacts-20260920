package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sort"
	"testing"
)

func newTestServer(t *testing.T) (*httptest.Server, string) {
	t.Helper()
	c := NewCluster()
	for _, id := range []string{"r1", "r2", "r3"} {
		c.Ensure(id)
	}
	srv := httptest.NewServer(Handler(c))
	t.Cleanup(srv.Close)
	return srv, ""
}

func doJSON(t *testing.T, method, url string, body any) map[string]any {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req, _ := http.NewRequest(method, url, &buf)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decoding %s %s: %v", method, url, err)
	}
	if resp.StatusCode >= 300 {
		t.Fatalf("%s %s -> %d: %v", method, url, resp.StatusCode, out)
	}
	return out
}

// siblingSig builds an order-independent signature of the sibling values.
func siblingSig(m map[string]any) string {
	raw, _ := m["siblings"].([]any)
	vals := make([]string, 0, len(raw))
	for _, item := range raw {
		v, _ := item.(map[string]any)
		vals = append(vals, v["value"].(string))
	}
	sort.Strings(vals)
	out := ""
	for _, v := range vals {
		out += v + "|"
	}
	return out
}

func readKey(t *testing.T, base, rid, key string) map[string]any {
	t.Helper()
	return doJSON(t, "GET", base+"/replicas/"+rid+"/keys/"+key, nil)
}

// End-to-end: partition -> double write -> sync -> resolve -> stale late write.
func TestHTTPPartitionSyncResolveStale(t *testing.T) {
	srv, _ := newTestServer(t)
	base := srv.URL

	// Isolated writes while the replicas are disconnected.
	doJSON(t, "PUT", base+"/replicas/r1/keys/cfg", map[string]string{"value": "red"})
	doJSON(t, "PUT", base+"/replicas/r2/keys/cfg", map[string]string{"value": "blue"})

	// Before sync each replica only sees its own value.
	if sig := siblingSig(readKey(t, base, "r1", "cfg")); sig != "red|" {
		t.Fatalf("r1 before sync = %q", sig)
	}

	// Heal the partition in both directions.
	doJSON(t, "POST", base+"/sync", map[string]string{"from": "r2", "to": "r1"})
	doJSON(t, "POST", base+"/sync", map[string]string{"from": "r1", "to": "r2"})

	s1 := siblingSig(readKey(t, base, "r1", "cfg"))
	s2 := siblingSig(readKey(t, base, "r2", "cfg"))
	if s1 != "blue|red|" || s2 != "blue|red|" {
		t.Fatalf("after sync siblings = %q / %q", s1, s2)
	}

	// Capture the two clocks to cite as merge context.
	raw := readKey(t, base, "r1", "cfg")["siblings"].([]any)
	context := []map[string]int64{}
	for _, item := range raw {
		v := item.(map[string]any)
		clock := map[string]int64{}
		for k, n := range v["clock"].(map[string]any) {
			clock[k] = int64(n.(float64))
		}
		context = append(context, clock)
	}
	if len(context) != 2 {
		t.Fatalf("want 2 clocks for context, got %d", len(context))
	}

	// Explicit merge on r1.
	doJSON(t, "POST", base+"/replicas/r1/keys/cfg/resolve",
		map[string]any{"value": "purple", "context": context})
	if sig := siblingSig(readKey(t, base, "r1", "cfg")); sig != "purple|" {
		t.Fatalf("after resolve r1 = %q", sig)
	}

	// Propagate merge to r2; r2's old siblings must be pruned.
	doJSON(t, "POST", base+"/sync", map[string]string{"from": "r1", "to": "r2"})
	if sig := siblingSig(readKey(t, base, "r2", "cfg")); sig != "purple|" {
		t.Fatalf("after merge sync r2 = %q", sig)
	}

	// Late re-delivery of the pre-merge "blue" version must be ignored.
	blueClock := map[string]int64{}
	// Find which context clock is blue's: just take r2-originated one
	// (component r2=1, r1 absent).
	for _, c := range context {
		if _, ok := c["r1"]; !ok {
			blueClock = c
		}
	}
	res := doJSON(t, "POST", base+"/replicas/r2/receive/cfg",
		map[string]any{"value": "blue", "clock": blueClock, "origin": "r2"})
	if res["admitted"] != false {
		t.Fatalf("late stale write was admitted: %v", res)
	}
	if sig := siblingSig(readKey(t, base, "r2", "cfg")); sig != "purple|" {
		t.Fatalf("stale write resurrected an old sibling: %q", sig)
	}
}

// Duplicate point-to-point messages and duplicate syncs are harmless.
func TestHTTPDuplicates(t *testing.T) {
	srv, _ := newTestServer(t)
	base := srv.URL

	doJSON(t, "PUT", base+"/replicas/r1/keys/k", map[string]string{"value": "a"})
	v := doJSON(t, "GET", base+"/replicas/r1/keys/k", nil)["siblings"].([]any)[0].(map[string]any)
	msg := map[string]any{
		"value":  v["value"],
		"clock":  v["clock"],
		"origin": v["origin"],
	}

	first := doJSON(t, "POST", base+"/replicas/r2/receive/k", msg)
	if first["admitted"] != true {
		t.Fatal("first message must be admitted")
	}
	dup := doJSON(t, "POST", base+"/replicas/r2/receive/k", msg)
	if dup["admitted"] != false {
		t.Fatal("duplicate message must be rejected")
	}
	// Full-sync duplicates too.
	for i := 0; i < 2; i++ {
		r := doJSON(t, "POST", base+"/sync", map[string]string{"from": "r1", "to": "r2"})
		if r["versions_admitted"] != float64(0) {
			t.Fatalf("sync #%d unexpectedly admitted versions: %v", i+2, r)
		}
	}
	if sig := siblingSig(readKey(t, base, "r2", "k")); sig != "a|" {
		t.Fatalf("r2 corrupted by duplicates: %q", sig)
	}
}

// resolve rejects context clocks that aren't current siblings.
func TestHTTPResolveUnknownContext(t *testing.T) {
	srv, _ := newTestServer(t)
	base := srv.URL

	doJSON(t, "PUT", base+"/replicas/r1/keys/k", map[string]string{"value": "a"})
	body := map[string]any{
		"value":   "x",
		"context": []map[string]int64{{"ghost": 42}},
	}
	buf, _ := json.Marshal(body)
	req, _ := http.NewRequest("POST", base+"/replicas/r1/keys/k/resolve", bytes.NewReader(buf))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("want 409 for unknown context, got %d", resp.StatusCode)
	}
}

func TestHTTPUnknownReplica(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/replicas/nope/keys")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("want 404, got %d", resp.StatusCode)
	}
}
