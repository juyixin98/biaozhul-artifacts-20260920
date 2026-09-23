package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

type apiClient struct {
	t *testing.T
	h http.Handler
}

func (c *apiClient) do(method, path string, body any, wantStatus int) map[string]any {
	c.t.Helper()
	var rdr *bytes.Buffer
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			c.t.Fatal(err)
		}
		rdr = bytes.NewBuffer(b)
	} else {
		rdr = bytes.NewBuffer(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	rec := httptest.NewRecorder()
	c.h.ServeHTTP(rec, req)
	if rec.Code != wantStatus {
		c.t.Fatalf("%s %s: status = %d want %d, body: %s", method, path, rec.Code, wantStatus, rec.Body.String())
	}
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			c.t.Fatalf("invalid JSON: %v\n%s", err, rec.Body.String())
		}
	}
	return out
}

func intVal(m map[string]any, key string) int {
	v, ok := m[key].(float64)
	if !ok {
		return -1
	}
	return int(v)
}

func TestHTTPFullLifecycle(t *testing.T) {
	c := &apiClient{t: t, h: newServer().routes()}

	// Health.
	c.do("GET", "/healthz", nil, http.StatusOK)

	// Revision 0 exists and is an empty ring.
	ring0 := c.do("GET", "/v1/ring?revision=0", nil, http.StatusOK)
	if intVal(ring0, "node_count") != 0 || intVal(ring0, "revision") != 0 {
		t.Fatalf("revision 0 not empty: %v", ring0)
	}

	// Routing on an empty ring fails with 503.
	c.do("GET", "/v1/route?key=alpha", nil, http.StatusServiceUnavailable)

	// Add three nodes, revisions tick to 1..3.
	for _, id := range []string{"a", "b", "c"} {
		resp := c.do("POST", "/v1/nodes", map[string]any{"id": id, "weight": 1}, http.StatusCreated)
		if intVal(resp, "node_count") == 0 {
			t.Fatal("node not added")
		}
	}
	ring3 := c.do("GET", "/v1/ring", nil, http.StatusOK)
	if intVal(ring3, "revision") != 3 {
		t.Fatalf("latest revision = %v", ring3["revision"])
	}

	// Duplicate add same weight is idempotent (200, no new revision).
	same := c.do("POST", "/v1/nodes", map[string]any{"id": "a", "weight": 1}, http.StatusOK)
	if intVal(same, "revision") != 3 {
		t.Fatalf("idempotent add created a revision: %v", same["revision"])
	}
	// Duplicate add different weight conflicts.
	c.do("POST", "/v1/nodes", map[string]any{"id": "a", "weight": 9}, http.StatusBadRequest)

	// Route alpha now and remember the owner.
	r1 := c.do("GET", "/v1/route?key=alpha&revision=3", nil, http.StatusOK)
	owner1, _ := r1["node"].(string)
	if owner1 == "" {
		t.Fatal("no node in route response")
	}
	hashHex, _ := r1["hash_hex"].(string)
	if len(hashHex) != 18 {
		t.Fatalf("bad hash_hex: %q", hashHex)
	}

	// Batch routing agrees with single routing.
	batch := c.do("POST", "/v1/route/batch", map[string]any{"keys": []string{"alpha", "beta", "gamma"}}, http.StatusOK)
	routes, _ := batch["routes"].([]any)
	first, _ := routes[0].(map[string]any)
	if first["node"] != owner1 || first["key"] != "alpha" {
		t.Fatalf("batch disagrees: %+v", first)
	}

	// Add a fourth node -> revision 4.
	c.do("POST", "/v1/nodes", map[string]any{"id": "d", "weight": 1}, http.StatusCreated)
	r2 := c.do("GET", "/v1/route?key=alpha&revision=4", nil, http.StatusOK)
	owner2, _ := r2["node"].(string)

	// The plan between revisions 3 and 4 must be consistent with what the two
	// route responses say about alpha.
	plan := c.do("GET", "/v1/plans?from=3&to=4", nil, http.StatusOK)
	moved := plan["moved"].([]any)
	stayed := plan["stayed"].([]any)
	if len(moved) == 0 {
		t.Fatal("adding a node must produce moves")
	}
	if intVal(plan, "to_revision") != 4 || intVal(plan, "from_revision") != 3 {
		t.Fatal("plan revisions wrong")
	}
	movedLen, _ := plan["moved_length"].(string)
	totalLen, _ := plan["total_length"].(string)
	if movedLen == "" || totalLen != "18446744073709551616" {
		t.Fatalf("plan lengths wrong: %s / %s", movedLen, totalLen)
	}
	// Locate alpha's hash inside the arcs and check the move/stay decision.
	h := HashKey("alpha")
	found := false
	for _, mv := range moved {
		m := mv.(map[string]any)
		arc := m["arc"].(map[string]any)
		a := arcFromJSON(arc)
		if a.Contains(h) {
			if m["from"] != owner1 || m["to"] != owner2 {
				t.Fatalf("plan move for alpha %v->%v but routes say %s->%s",
					m["from"], m["to"], owner1, owner2)
			}
			found = true
		}
	}
	for _, st := range stayed {
		s := st.(map[string]any)
		arc := s["arc"].(map[string]any)
		if arcFromJSON(arc).Contains(h) && owner1 != owner2 {
			t.Fatal("alpha changed owner but lies in a stayed arc")
		}
	}
	if owner1 != owner2 && !found {
		t.Fatal("alpha moved but no moved arc contains its hash")
	}

	// Default plan endpoints: latest pair (3->4 was explicit; latest is now 4).
	c.do("GET", "/v1/plans", nil, http.StatusOK)

	// Replace with a weighted configuration -> revision 5.
	repl := c.do("POST", "/v1/ring/replace", map[string]any{
		"nodes": []map[string]any{
			{"id": "a", "weight": 4},
			{"id": "b", "weight": 1},
			{"id": "c", "weight": 1},
			{"id": "d", "weight": 2},
		},
	}, http.StatusCreated)
	if intVal(repl, "revision") != 5 {
		t.Fatalf("replace revision = %v", repl["revision"])
	}

	// Delete a node -> revision 6.
	del := c.do("DELETE", "/v1/nodes/b", nil, http.StatusCreated)
	if intVal(del, "node_count") != 3 {
		t.Fatal("delete did not reduce node count")
	}
	c.do("DELETE", "/v1/nodes/ghost", nil, http.StatusBadRequest)

	// Plans must be available between arbitrary retained revisions.
	c.do("GET", "/v1/plans?from=0&to=6", nil, http.StatusOK)
	c.do("GET", "/v1/plans?from=6&to=0", nil, http.StatusBadRequest)
	c.do("GET", "/v1/plans?from=99&to=100", nil, http.StatusNotFound)

	// Validation errors.
	c.do("POST", "/v1/nodes", map[string]any{"id": "", "weight": 1}, http.StatusBadRequest)
	c.do("POST", "/v1/nodes", map[string]any{"id": "x", "weight": 0}, http.StatusBadRequest)
	c.do("POST", "/v1/nodes", map[string]any{"id": "hash#", "weight": 1}, http.StatusBadRequest)
	c.do("POST", "/v1/nodes", "not-json", http.StatusBadRequest)
	c.do("POST", "/v1/nodes", map[string]any{"id": "x", "weight": 1, "extra": true}, http.StatusBadRequest)
	c.do("POST", "/v1/route/batch", map[string]any{"keys": []string{}}, http.StatusBadRequest)
	c.do("GET", "/v1/route", nil, http.StatusBadRequest)
	c.do("GET", "/v1/ring?revision=-1", nil, http.StatusBadRequest)
	c.do("GET", "/nope", nil, http.StatusNotFound)

	// Revision listing.
	revs := c.do("GET", "/v1/revisions", nil, http.StatusOK)
	list := revs["revisions"].([]any)
	if len(list) != 7 {
		t.Fatalf("expected 7 revisions (0..6), got %d", len(list))
	}
}

func arcFromJSON(m map[string]any) Arc {
	return Arc{
		StartExclusive: uint64(m["start_exclusive"].(float64)),
		EndInclusive:   uint64(m["end_inclusive"].(float64)),
		Wrap:           m["wrap"].(bool),
	}
}

func TestHTTPLargeKeySetMatchesPlan(t *testing.T) {
	srv := newServer()
	c := &apiClient{t: t, h: srv.routes()}
	for _, id := range []string{"alpha", "bravo", "charlie", "delta", "echo"} {
		c.do("POST", "/v1/nodes", map[string]any{"id": id, "weight": 1 + len(id)%3}, http.StatusCreated)
	}
	before := srv.latest().revision
	c.do("POST", "/v1/nodes", map[string]any{"id": "foxtrot", "weight": 2}, http.StatusCreated)
	after := srv.latest().revision

	planResp := c.do("GET", fmt.Sprintf("/v1/plans?from=%d&to=%d", before, after), nil, http.StatusOK)
	plan := BuildPlan(before, after, srv.snapshots[before].ring, srv.snapshots[after].ring)

	// JSON plan must equal the in-memory plan's moved length.
	if ml, _ := planResp["moved_length"].(string); ml != plan.MovedLength {
		t.Fatalf("JSON moved_length %s != %s", ml, plan.MovedLength)
	}

	// 30k random keys: route both revisions, verify the move decision agrees
	// with arc containment AND that stayed keys kept their node.
	rng := rand.New(rand.NewSource(31337))
	keys := make([]string, 2000)
	for i := range keys {
		keys[i] = fmt.Sprintf("user:%d:%s", rng.Int63(), randKey(rng))
	}
	batchReq := map[string]any{"keys": keys, "revision": before}
	bOld := c.do("POST", "/v1/route/batch", batchReq, http.StatusOK)
	batchReq["revision"] = after
	bNew := c.do("POST", "/v1/route/batch", batchReq, http.StatusOK)
	oldRoutes := bOld["routes"].([]any)
	newRoutes := bNew["routes"].([]any)
	for i, k := range keys {
		om := oldRoutes[i].(map[string]any)
		nm := newRoutes[i].(map[string]any)
		oo, _ := om["node"].(string)
		nn, _ := nm["node"].(string)
		h := HashKey(k)
		_, _, inMoved := containingArc(t, plan, h)
		if (oo != nn) != inMoved {
			t.Fatalf("key %s: routes %s->%s but plan moved=%v", k, oo, nn, inMoved)
		}
	}
}

func TestHTTPConcurrentMutations(t *testing.T) {
	srv := newServer()
	h := srv.routes()
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			body, _ := json.Marshal(map[string]any{"id": fmt.Sprintf("n%d", i), "weight": 1})
			req := httptest.NewRequest("POST", "/v1/nodes", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)
			if rec.Code != http.StatusCreated {
				t.Errorf("add n%d status %d", i, rec.Code)
			}
		}(i)
	}
	wg.Wait()
	final := srv.latest()
	if final.revision != 20 {
		t.Fatalf("expected 20 revisions, got %d", final.revision)
	}
	if len(final.ring.Nodes()) != 20 {
		t.Fatalf("expected 20 nodes, got %d", len(final.ring.Nodes()))
	}
	// Revision 0 -> latest must still tile the circle.
	plan := BuildPlan(0, 20, srv.snapshots[0].ring, final.ring)
	assertTilesCircle(t, plan)
}
