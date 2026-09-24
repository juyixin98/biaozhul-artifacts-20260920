package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"testing"
	"time"

	"orset/internal/orset"
	"orset/internal/server"
)

func startReplica(t *testing.T, id string) (*orset.ORSet, string, func()) {
	t.Helper()
	set := orset.New(id)
	srv := server.New(set, log.New(io.Discard, "", 0))
	ts := httptest.NewServer(srv.Handler())
	return set, ts.URL, ts.Close
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, method, url, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, url, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func elements(t *testing.T, base string) []string {
	t.Helper()
	_, out := doJSON(t, http.MethodGet, base+"/elements", nil)
	raw := out["elements"].([]any)
	got := make([]string, len(raw))
	for i, v := range raw {
		got[i] = v.(string)
	}
	sort.Strings(got)
	return got
}

func stateOf(t *testing.T, base string) orset.State {
	t.Helper()
	_, out := doJSON(t, http.MethodGet, base+"/state", nil)
	return toState(out["state"].(map[string]any))
}

func toState(m map[string]any) orset.State {
	st := orset.State{Add: map[string][]string{}, Tombstones: map[string][]string{}}
	fill := func(key string, dst map[string][]string) {
		if raw, ok := m[key].(map[string]any); ok {
			for e, tags := range raw {
				for _, tg := range tags.([]any) {
					dst[e] = append(dst[e], tg.(string))
				}
			}
		}
	}
	fill("add", st.Add)
	fill("tombstones", st.Tombstones)
	return st
}

func syncPeers(t *testing.T, base string, peers ...string) {
	t.Helper()
	status, out := doJSON(t, http.MethodPost, base+"/sync",
		map[string]any{"peers": peers})
	if status != http.StatusOK || out["ok"] != true {
		t.Fatalf("sync from %s -> %v failed: %v", base, peers, out)
	}
}

func TestBasicEndpoints(t *testing.T) {
	_, base, closeFn := startReplica(t, "t1")
	defer closeFn()

	if code, out := doJSON(t, http.MethodGet, base+"/health", nil); code != 200 || out["replicaId"] != "t1" {
		t.Fatalf("health = %d %v", code, out)
	}

	// remove of nothing -> 204, harmless
	if code, _ := doJSON(t, http.MethodDelete, base+"/elements/ghost", nil); code != http.StatusNoContent {
		t.Fatalf("remove-missing status = %d, want 204", code)
	}

	if code, out := doJSON(t, http.MethodPost, base+"/elements", map[string]string{"element": "x"}); code != 200 || out["tag"] == "" {
		t.Fatalf("add x: %d %v", code, out)
	}
	if got := elements(t, base); !reflect.DeepEqual(got, []string{"x"}) {
		t.Fatalf("elements = %v", got)
	}
	if code, _ := doJSON(t, http.MethodDelete, base+"/elements/x", nil); code != 200 {
		t.Fatalf("delete x status = %d", code)
	}
	if got := elements(t, base); len(got) != 0 {
		t.Fatalf("elements after remove = %v", got)
	}

	// validation: empty element and malformed / unknown JSON
	if code, _ := doJSON(t, http.MethodPost, base+"/elements", map[string]string{"element": ""}); code != http.StatusBadRequest {
		t.Fatalf("empty element status = %d", code)
	}
	if code, _ := doJSON(t, http.MethodPost, base+"/elements", map[string]string{"bogus": "x"}); code != http.StatusBadRequest {
		t.Fatalf("unknown field should be rejected, got %d", code)
	}
	if code, _ := doJSON(t, http.MethodPost, base+"/state", map[string]any{"state": "not-a-state"}); code != http.StatusBadRequest {
		t.Fatalf("bad state should be 400, got %d", code)
	}
}

func TestOpEndpointIdempotentReorder(t *testing.T) {
	_, base, closeFn := startReplica(t, "op1")
	defer closeFn()

	add := map[string]any{"type": "add", "element": "x", "tags": []string{"tag-1"}}
	rem := map[string]any{"type": "remove", "element": "x", "tags": []string{"tag-1"}}

	// Remove arrives BEFORE the add (reordered, duplicated).
	for i := 0; i < 3; i++ {
		if code, out := doJSON(t, http.MethodPost, base+"/ops", rem); code != 200 {
			t.Fatalf("remove op: %d %v", code, out)
		}
	}
	if code, out := doJSON(t, http.MethodPost, base+"/ops", add); code != 200 {
		t.Fatalf("add op: %d %v", code, out)
	}
	if got := elements(t, base); len(got) != 0 {
		t.Fatalf("x must remain removed after late add of same tag, got %v", got)
	}

	// Bad op type rejected.
	if code, _ := doJSON(t, http.MethodPost, base+"/ops", map[string]any{"type": "explode", "element": "x"}); code != http.StatusBadRequest {
		t.Fatalf("bad op type should be 400")
	}
}

// fullMesh lets every replica exchange state with every other replica once,
// in the given per-node order. Each /sync is pull+push, so after the whole
// sequence every pair has merged both directions.
func fullMesh(t *testing.T, urls []string, order []int) {
	t.Helper()
	for _, i := range order {
		var peers []string
		for j, u := range urls {
			if j != i {
				peers = append(peers, u)
			}
		}
		syncPeers(t, urls[i], peers...)
	}
}

// TestThreeReplicaPartitionHeal is the headline acceptance test: three
// replicas start converged, diverge while partitioned, and heal. All 6 node
// orders of healing must converge to the same set and identical CRDT state.
// A duplicate sync round afterwards must be a no-op.
func TestThreeReplicaPartitionHeal(t *testing.T) {
	for trial, order := range perm3() {
		t.Run(fmt.Sprintf("heal-order-%v", order), func(t *testing.T) {
			sets := make([]*orset.ORSet, 3)
			urls := make([]string, 3)
			closes := make([]func(), 3)
			for i := range sets {
				sets[i], urls[i], closes[i] = startReplica(t,
					fmt.Sprintf("r%d-%d", i, trial))
			}
			defer func() {
				for _, c := range closes {
					c()
				}
			}()

			// Common history: r1 adds a,b,c and everyone fully syncs.
			for _, e := range []string{"a", "b", "c"} {
				if code, out := doJSON(t, http.MethodPost, urls[0]+"/elements",
					map[string]string{"element": e}); code != 200 {
					t.Fatalf("add: %v", out)
				}
			}
			fullMesh(t, urls, []int{0, 1, 2})
			for i, u := range urls {
				if got := elements(t, u); !reflect.DeepEqual(got, []string{"a", "b", "c"}) {
					t.Fatalf("pre-partition replica %d = %v", i, got)
				}
			}

			// Network partitions: no syncs happen during these edits.
			// r1: removes a
			// r2: CONCURRENTLY re-adds a (fresh tag never observed by r1), adds d
			// r3: removes b (which all replicas observed), adds e
			doJSON(t, http.MethodDelete, urls[0]+"/elements/a", nil)
			doJSON(t, http.MethodPost, urls[1]+"/elements", map[string]string{"element": "a"})
			doJSON(t, http.MethodPost, urls[1]+"/elements", map[string]string{"element": "d"})
			doJSON(t, http.MethodDelete, urls[2]+"/elements/b", nil)
			doJSON(t, http.MethodPost, urls[2]+"/elements", map[string]string{"element": "e"})

			// Heal in the permuted node order.
			fullMesh(t, urls, order)

			// a: old tag dead on r1, but r2's concurrent fresh tag
			// survives -> add-wins. b: observed-removed everywhere.
			// c: untouched. d,e: partition-time adds merge in.
			want := []string{"a", "c", "d", "e"}
			final := make([]orset.State, 3)
			for i, u := range urls {
				if got := elements(t, u); !reflect.DeepEqual(got, want) {
					t.Fatalf("post-heal replica %d = %v, want %v (order %v)", i, got, want, order)
				}
				final[i] = stateOf(t, u)
			}
			for i := 1; i < 3; i++ {
				if !orset.Equal(final[0], final[i]) {
					t.Fatalf("replicas converged to different CRDT states:\n%+v\n%+v", final[0], final[i])
				}
			}

			// Duplicate sync rounds (retries) change nothing.
			fullMesh(t, urls, order)
			fullMesh(t, urls, []int{2, 0, 1})
			for i, u := range urls {
				if got := elements(t, u); !reflect.DeepEqual(got, want) {
					t.Fatalf("post-duplicate-sync replica %d = %v", i, got)
				}
				if now := stateOf(t, u); !orset.Equal(now, final[0]) {
					t.Fatalf("duplicate sync altered state on replica %d", i)
				}
			}
		})
	}
}

// TestGCEndToEnd verifies the coordinated GC over HTTP: after convergence the
// a/b tombstones become eligible, a /gc round purges them on every node, and
// a never-synced 4th replica blocks GC (fail closed).
func TestGCEndToEnd(t *testing.T) {
	urls := make([]string, 3)
	closes := make([]func(), 3)
	for i := range urls {
		_, urls[i], closes[i] = startReplica(t, fmt.Sprintf("gcr%d", i))
	}
	defer func() {
		for _, c := range closes {
			c()
		}
	}()

	doJSON(t, http.MethodPost, urls[0]+"/elements", map[string]string{"element": "a"})
	doJSON(t, http.MethodPost, urls[0]+"/elements", map[string]string{"element": "b"})
	fullMesh(t, urls, []int{0, 1, 2})
	doJSON(t, http.MethodDelete, urls[1]+"/elements/a", nil)
	doJSON(t, http.MethodDelete, urls[2]+"/elements/b", nil)
	fullMesh(t, urls, []int{2, 1, 0}) // propagate tombstones

	// Dry run: both tombstones eligible.
	code, out := doJSON(t, http.MethodGet,
		fmt.Sprintf("%s/gc/eligible?peer=%s&peer=%s", urls[0], urls[1], urls[2]), nil)
	if code != 200 || out["eligibleCount"].(float64) != 2 {
		t.Fatalf("eligible dry run: %d %v", code, out)
	}

	// Coordinated run.
	code, out = doJSON(t, http.MethodPost, urls[0]+"/gc",
		map[string]any{"peers": []string{urls[1], urls[2]}})
	if code != 200 || out["totalPurged"].(float64) != 6 { // 2 tags x 3 nodes
		t.Fatalf("gc run: %d %v", code, out)
	}
	for i, u := range urls {
		_, stOut := doJSON(t, http.MethodGet, u+"/debug/stats", nil)
		if stOut["tombstoneTags"].(float64) != 0 {
			t.Fatalf("replica %d still holds tombstones: %v", i, stOut)
		}
		if got := elements(t, u); !reflect.DeepEqual(got, []string{}) {
			t.Fatalf("replica %d elements = %v after GC (a,b were both removed)", i, got)
		}
	}

	// A newborn / offline replica that never received the history must block
	// reclamation of anything.
	_, fresh, closeFresh := startReplica(t, "fresh")
	defer closeFresh()
	// Fresh node has no tombstones: eligible across all 4 = nothing.
	code, out = doJSON(t, http.MethodGet,
		fmt.Sprintf("%s/gc/eligible?peer=%s&peer=%s&peer=%s", urls[0], urls[1], urls[2], fresh), nil)
	if code != 200 || out["eligibleCount"].(float64) != 0 {
		t.Fatalf("unsynced replica must block GC: %d %v", code, out)
	}
}

// perm3 returns all 6 permutations of [0,1,2].
func perm3() [][]int {
	base := [][]int{
		{0, 1, 2}, {0, 2, 1}, {1, 0, 2}, {1, 2, 0}, {2, 0, 1}, {2, 1, 0},
	}
	return base
}
