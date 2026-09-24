package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"gangscheduler/gang"
)

func newTestServer(t *testing.T) (*httptest.Server, *gang.Scheduler) {
	t.Helper()
	sched := gang.NewScheduler()
	sched.Start(context.Background())
	t.Cleanup(func() {
		sched.Stop()
	})
	srv := &Server{Sched: sched}
	ts := httptest.NewServer(srv.NewRouter())
	t.Cleanup(ts.Close)
	return ts, sched
}

func postJSON(t *testing.T, ts *httptest.Server, path string, body any) (int, map[string]any) {
	t.Helper()
	b, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(ts.URL+path, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decodeResp(t, resp)
}

func getJSON(t *testing.T, ts *httptest.Server, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(ts.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return decodeResp(t, resp)
}

func decodeResp(t *testing.T, resp *http.Response) (int, map[string]any) {
	t.Helper()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode %d: %v", resp.StatusCode, err)
	}
	return resp.StatusCode, out
}

func mustStatus(t *testing.T, got, want int, body map[string]any) {
	t.Helper()
	if got != want {
		t.Fatalf("status = %d, want %d: %v", got, want, body)
	}
}

func addThreeNodes(t *testing.T, ts *httptest.Server) {
	t.Helper()
	nodes := []map[string]any{
		{"id": "n1", "zone": "z1", "capacity": 1, "labels": map[string]string{"role": "a"}},
		{"id": "n2", "zone": "z1", "capacity": 1, "labels": map[string]string{"role": "shared"}},
		{"id": "n3", "zone": "z2", "capacity": 1, "labels": map[string]string{"role": "b"}},
	}
	for _, n := range nodes {
		if st, body := postJSON(t, ts, "/nodes", n); st != http.StatusCreated {
			t.Fatalf("add node %v: %d %v", n["id"], st, body)
		}
	}
}

// End-to-end acceptance scenario driven purely over HTTP: the two competing
// gangs, the offline node between reserve and commit, and the final
// all-or-nothing recovery.
func TestHTTPAcceptanceFlow(t *testing.T) {
	ts, _ := newTestServer(t)
	addThreeNodes(t, ts)

	if st, b := getJSON(t, ts, "/healthz"); st != 200 || b["status"] != "ok" {
		t.Fatalf("health: %d %v", st, b)
	}

	// gA reserves n1+n2.
	st, b := postJSON(t, ts, "/gangs", map[string]any{
		"id": "gA", "min_nodes": 2, "ttl_millis": 60000,
		"tasks": []map[string]any{
			{"name": "a1", "node_selector": map[string]string{"role": "a"}},
			{"name": "a2", "node_selector": map[string]string{"role": "shared"}},
		},
	})
	mustStatus(t, st, 201, b)
	if b["gang"].(map[string]any)["status"] != "reserved" {
		t.Fatalf("gA not reserved: %v", b)
	}
	planA := b["plan"].(map[string]any)
	verA := int64(planA["version"].(float64))

	// gB wants n2+n3 -> must wait, plan:null.
	st, b = postJSON(t, ts, "/gangs", map[string]any{
		"id": "gB", "min_nodes": 2, "ttl_millis": 60000,
		"tasks": []map[string]any{
			{"name": "b1", "node_selector": map[string]string{"role": "shared"}},
			{"name": "b2", "node_selector": map[string]string{"role": "b"}},
		},
	})
	mustStatus(t, st, 201, b)
	if b["gang"].(map[string]any)["status"] != "waiting" || b["plan"] != nil {
		t.Fatalf("gB should wait with no plan: %v", b)
	}

	// n2 goes offline while gA holds it.
	st, b = postJSON(t, ts, "/nodes/n2/state", map[string]any{"online": false})
	mustStatus(t, st, 200, b)

	// gA commit: 410 Gone, nothing started.
	st, b = postJSON(t, ts, "/gangs/gA/plans/"+planA["id"].(string)+"/commit",
		map[string]any{"version": verA})
	mustStatus(t, st, 410, b)

	// State confirms no running/held slots leaked on n1.
	_, state := getJSON(t, ts, "/state")
	for _, nv := range state["nodes"].([]any) {
		n := nv.(map[string]any)
		if n["running_slots"].(float64) != 0 || n["held_slots"].(float64) != 0 {
			t.Fatalf("node %s leaked slots after abort: %v", n["id"], n)
		}
	}

	// n2 back: gA reserves again, commits, completes; gB then gets n2+n3.
	st, _ = postJSON(t, ts, "/nodes/n2/state", map[string]any{"online": true})
	mustStatus(t, st, 200, nil)

	st, b = getJSON(t, ts, "/gangs/gA")
	mustStatus(t, st, 200, b)
	if b["gang"].(map[string]any)["status"] != "reserved" {
		t.Fatalf("gA should reserve again: %v", b)
	}
	planA2 := b["plan"].(map[string]any)
	verA2 := int64(planA2["version"].(float64))
	st, b = postJSON(t, ts, "/gangs/gA/plans/"+planA2["id"].(string)+"/commit",
		map[string]any{"version": verA2})
	mustStatus(t, st, 200, b)

	// Running node cannot go offline: 409.
	st, b = postJSON(t, ts, "/nodes/n2/state", map[string]any{"online": false})
	mustStatus(t, st, 409, b)

	st, _ = postJSON(t, ts, "/gangs/gA/complete", map[string]any{})
	mustStatus(t, st, 200, nil)

	// gB auto-reserved when gA freed the nodes.
	st, b = getJSON(t, ts, "/gangs/gB")
	mustStatus(t, st, 200, b)
	if b["gang"].(map[string]any)["status"] != "reserved" {
		t.Fatalf("gB should now be reserved: %v", b)
	}
	planB := b["plan"].(map[string]any)
	gotNodes := planB["nodes"].([]any)
	if len(gotNodes) != 2 {
		t.Fatalf("gB nodes = %v", gotNodes)
	}
	verB := int64(planB["version"].(float64))
	st, b = postJSON(t, ts, "/gangs/gB/plans/"+planB["id"].(string)+"/commit",
		map[string]any{"version": verB})
	mustStatus(t, st, 200, b)
	if b["gang"].(map[string]any)["status"] != "running" {
		t.Fatalf("gB not running: %v", b)
	}
}

// TTL is enforced by the real background reaper (real wall clock, short TTL).
func TestHTTPReservationTTLExpiresInBackground(t *testing.T) {
	sched := gang.NewScheduler(gang.WithSweepInterval(20*time.Millisecond),
		gang.WithDefaultTTL(80*time.Millisecond))
	sched.Start(context.Background())
	t.Cleanup(sched.Stop)
	ts := httptest.NewServer((&Server{Sched: sched}).NewRouter())
	t.Cleanup(ts.Close)

	postJSON(t, ts, "/nodes", map[string]any{"id": "n1", "capacity": 1})
	st, b := postJSON(t, ts, "/gangs", map[string]any{
		"id": "g1", "min_nodes": 1,
		"tasks": []map[string]any{{"name": "t1"}},
	})
	mustStatus(t, st, 201, b)
	if b["gang"].(map[string]any)["status"] != "reserved" {
		t.Fatalf("g1: %v", b)
	}

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		_, gb := getJSON(t, ts, "/gangs/g1")
		if gb["gang"].(map[string]any)["status"] == "expired" {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	_, gb := getJSON(t, ts, "/gangs/g1")
	if gb["gang"].(map[string]any)["status"] != "expired" {
		t.Fatalf("g1 was not expired by reaper: %v", gb)
	}
	_, nv := getJSON(t, ts, "/nodes/n1")
	if nv["held_slots"].(float64) != 0 {
		t.Fatalf("n1 still held after ttl: %v", nv)
	}
}

// Wrong optimistic version over HTTP -> 409 and no slots started.
func TestHTTPStaleVersionConflict(t *testing.T) {
	ts, _ := newTestServer(t)
	postJSON(t, ts, "/nodes", map[string]any{"id": "n1", "capacity": 1})
	postJSON(t, ts, "/nodes", map[string]any{"id": "n2", "capacity": 1})
	_, b := postJSON(t, ts, "/gangs", map[string]any{
		"id": "g1", "min_nodes": 2,
		"tasks": []map[string]any{{"name": "a"}, {"name": "b"}},
	})
	plan := b["plan"].(map[string]any)
	st, b := postJSON(t, ts, "/gangs/g1/plans/"+plan["id"].(string)+"/commit",
		map[string]any{"version": int64(plan["version"].(float64)) - 1})
	mustStatus(t, st, 409, b)
	if !strings.Contains(b["error"].(string), "version mismatch") {
		t.Fatalf("unexpected error: %v", b)
	}
	_, state := getJSON(t, ts, "/state")
	for _, nv := range state["nodes"].([]any) {
		n := nv.(map[string]any)
		if n["running_slots"].(float64) != 0 {
			t.Fatalf("node %s started after failed commit", n["id"])
		}
	}
}

// Error mapping and input validation.
func TestHTTPErrors(t *testing.T) {
	ts, _ := newTestServer(t)

	if st, _ := getJSON(t, ts, "/nodes/nope"); st != 404 {
		t.Fatalf("missing node: %d", st)
	}
	if st, _ := getJSON(t, ts, "/gangs/nope"); st != 404 {
		t.Fatalf("missing gang: %d", st)
	}
	// Bad JSON / unknown fields -> 400.
	resp, err := http.Post(ts.URL+"/nodes", "application/json", strings.NewReader("{bad"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("malformed json: %d", resp.StatusCode)
	}
	resp, err = http.Post(ts.URL+"/nodes", "application/json",
		strings.NewReader(`{"id":"x","bogus":1}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("unknown field: %d", resp.StatusCode)
	}
	// min_nodes/task mismatch -> 400.
	st, b := postJSON(t, ts, "/gangs", map[string]any{
		"id": "g", "min_nodes": 3,
		"tasks": []map[string]any{{"name": "a"}},
	})
	mustStatus(t, st, 400, b)
	// Method not allowed via Go 1.22 pattern routing.
	req, _ := http.NewRequest(http.MethodDelete, ts.URL+"/state", nil)
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 405 {
		t.Fatalf("DELETE /state: %d, want 405", resp.StatusCode)
	}
}

// /retry and /release endpoint coverage incl. their error mappings.
func TestHTTPRetryAndRelease(t *testing.T) {
	ts, _ := newTestServer(t)
	postJSON(t, ts, "/nodes", map[string]any{"id": "n1", "capacity": 1})
	postJSON(t, ts, "/nodes", map[string]any{"id": "n2", "capacity": 1})

	// A 2-node gang pinned to a non-existent label: never schedulable here.
	st, b := postJSON(t, ts, "/gangs", map[string]any{
		"id": "gw", "min_nodes": 2,
		"tasks": []map[string]any{
			{"name": "a", "node_selector": map[string]string{"role": "rare"}},
			{"name": "b", "node_selector": map[string]string{"role": "rare"}},
		},
	})
	mustStatus(t, st, 201, b)

	// Retry while still unschedulable -> stays waiting, no plan.
	st, b = postJSON(t, ts, "/gangs/gw/retry", map[string]any{})
	mustStatus(t, st, 200, b)
	if b["gang"].(map[string]any)["status"] != "waiting" || b["plan"] != nil {
		t.Fatalf("retry without capacity: %v", b)
	}
	// Retry an unknown gang -> 404.
	if st, _ := postJSON(t, ts, "/gangs/ghost/retry", map[string]any{}); st != 404 {
		t.Fatalf("retry ghost: %d", st)
	}

	// A 1-node gang that reserves, then releases -> waiting, node freed;
	// retry reserves again.
	_, b = postJSON(t, ts, "/gangs", map[string]any{
		"id": "gr", "min_nodes": 1,
		"tasks": []map[string]any{{"name": "x"}},
	})
	plan := b["plan"].(map[string]any)
	node := plan["nodes"].([]any)[0].(string)
	st, b = postJSON(t, ts, "/gangs/gr/plans/"+plan["id"].(string)+"/release", map[string]any{})
	mustStatus(t, st, 200, b)
	if b["gang"].(map[string]any)["status"] != "waiting" {
		t.Fatalf("after release: %v", b)
	}
	_, nv := getJSON(t, ts, "/nodes/"+node)
	if nv["held_slots"].(float64) != 0 {
		t.Fatalf("node still held after release: %v", nv)
	}
	st, b = postJSON(t, ts, "/gangs/gr/retry", map[string]any{})
	mustStatus(t, st, 200, b)
	if b["gang"].(map[string]any)["status"] != "reserved" || b["plan"] == nil {
		t.Fatalf("retry did not reserve again: %v", b)
	}
	// Releasing an unknown plan -> 404; completing a non-running gang -> 409.
	if st, _ := postJSON(t, ts, "/gangs/gr/plans/nope/release", map[string]any{}); st != 404 {
		t.Fatalf("release unknown plan: %d", st)
	}
	if st, _ := postJSON(t, ts, "/gangs/gw/complete", map[string]any{}); st != 409 {
		t.Fatalf("complete waiting gang: %d", st)
	}
}

// Commit against an unknown gang/plan and toggling an unknown node's state.
func TestHTTPMoreErrorPaths(t *testing.T) {
	ts, _ := newTestServer(t)
	if st, _ := postJSON(t, ts, "/nodes/ghost/state", map[string]any{"online": false}); st != 404 {
		t.Fatalf("unknown node state: %d", st)
	}
	// Unknown sub-path under a gang -> method/pattern miss -> 405 plain text.
	resp, err := http.Post(ts.URL+"/gangs/g", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 405 {
		t.Fatalf("POST /gangs/g: %d, want 405", resp.StatusCode)
	}
	// Well-formed JSON but invalid gang spec -> 400.
	if st, _ := postJSON(t, ts, "/gangs", map[string]any{"id": "g", "min_nodes": 0}); st != 400 {
		t.Fatalf("invalid gang body: %d", st)
	}
	postJSON(t, ts, "/nodes", map[string]any{"id": "n1", "capacity": 1})
	_, b := postJSON(t, ts, "/gangs", map[string]any{
		"id": "g1", "min_nodes": 1,
		"tasks": []map[string]any{{"name": "t"}},
	})
	plan := b["plan"].(map[string]any)
	// Unknown gang id but valid plan -> 404.
	st, _ := postJSON(t, ts, "/gangs/ghost/plans/"+plan["id"].(string)+"/commit",
		map[string]any{"version": 1})
	if st != 404 {
		t.Fatalf("commit via wrong gang: %d", st)
	}
	// Duplicate node -> 409.
	if st, _ := postJSON(t, ts, "/nodes", map[string]any{"id": "n1", "capacity": 1}); st != 409 {
		t.Fatalf("duplicate node: %d", st)
	}
}

// Concurrency: many gangs hammering the API simultaneously must never
// overbook a node (checked under -race as well).
func TestHTTPConcurrentNoOverbook(t *testing.T) {
	ts, sched := newTestServer(t)
	for i := 0; i < 4; i++ {
		postJSON(t, ts, "/nodes", map[string]any{
			"id": "n" + string(rune('0'+i)), "capacity": 1,
		})
	}

	const n = 20
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := "g" + string(rune('A'+i))
			_, b := postJSON(t, ts, "/gangs", map[string]any{
				"id": id, "min_nodes": 2, "ttl_millis": 1000,
				"tasks": []map[string]any{{"name": "a"}, {"name": "b"}},
			})
			if b["gang"].(map[string]any)["status"] == "reserved" {
				plan := b["plan"].(map[string]any)
				ver := int64(plan["version"].(float64))
				st, _ := postJSON(t, ts,
					"/gangs/"+id+"/plans/"+plan["id"].(string)+"/commit",
					map[string]any{"version": ver})
				if st == 200 {
					postJSON(t, ts, "/gangs/"+id+"/complete", map[string]any{})
				}
			}
		}(i)
	}
	wg.Wait()

	st := sched.GetState()
	totalRunning := 0
	for _, nv := range st.Nodes {
		if nv.RunningSlots > nv.Capacity {
			t.Fatalf("node %s overbooked: %d > %d", nv.ID, nv.RunningSlots, nv.Capacity)
		}
		totalRunning += nv.RunningSlots
	}
	if totalRunning%2 != 0 {
		t.Fatalf("running slots = %d, gangs use 2 nodes each", totalRunning)
	}
}
