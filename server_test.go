package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sort"
	"testing"
)

// httpTest wraps an httptest server with small JSON helpers so the acceptance
// scenarios read exactly like the curl flows documented in the README.
type httpTest struct {
	t    *testing.T
	srv  *httptest.Server
	host string
}

func newHTTPTest(t *testing.T) *httpTest {
	t.Helper()
	srv := httptest.NewServer(NewServer(NewStore()))
	t.Cleanup(srv.Close)
	return &httpTest{t: t, srv: srv, host: srv.URL}
}

func (h *httpTest) do(method, path string, body any, dst any) int {
	h.t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			h.t.Fatalf("marshal request: %v", err)
		}
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, h.host+path, rdr)
	if err != nil {
		h.t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatalf("%s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	if dst != nil {
		if err := json.NewDecoder(resp.Body).Decode(dst); err != nil {
			h.t.Fatalf("decode response of %s %s: %v", method, path, err)
		}
	}
	return resp.StatusCode
}

func (h *httpTest) mustStatus(method, path string, body any, dst any, want int) {
	h.t.Helper()
	if got := h.do(method, path, body, dst); got != want {
		h.t.Fatalf("%s %s -> %d, want %d", method, path, got, want)
	}
}

func (h *httpTest) createReplica(id string) {
	h.mustStatus(http.MethodPost, "/replicas", map[string]string{"id": id}, nil, http.StatusCreated)
}

type writeResp struct {
	Written   Version   `json:"written"`
	Survivors []Version `json:"survivors"`
}

func (h *httpTest) put(replica, key, value string) Version {
	var resp writeResp
	h.mustStatus(http.MethodPut, "/replicas/"+replica+"/keys/"+key,
		map[string]string{"value": value}, &resp, http.StatusCreated)
	return resp.Written
}

type readResp struct {
	Versions []Version `json:"versions"`
}

func (h *httpTest) versionIDs(replica, key string) []string {
	var resp readResp
	h.mustStatus(http.MethodGet, "/replicas/"+replica+"/keys/"+key, nil, &resp, http.StatusOK)
	out := ids(resp.Versions)
	sort.Strings(out)
	return out
}

type deliverResp struct {
	Outcomes map[string]IncomingOutcome `json:"outcomes"`
}

func (h *httpTest) deliver(replica, key string, vs []Version) map[string]IncomingOutcome {
	var resp deliverResp
	h.mustStatus(http.MethodPost, "/replicas/"+replica+"/keys/"+key+"/messages",
		map[string]any{"versions": vs}, &resp, http.StatusOK)
	return resp.Outcomes
}

type mergeResp struct {
	Merged Version `json:"merged"`
}

func (h *httpTest) merge(replica, key, value string, context []string) Version {
	var resp mergeResp
	h.mustStatus(http.MethodPost, "/replicas/"+replica+"/keys/"+key+"/merge",
		map[string]any{"value": value, "context": context}, &resp, http.StatusCreated)
	return resp.Merged
}

func (h *httpTest) syncTwoWay(a, b string) {
	h.mustStatus(http.MethodPost, "/replicas/"+a+"/sync",
		map[string]string{"from": a, "to": b, "mode": "two-way"}, nil, http.StatusOK)
}

// Acceptance scenario 1: two replicas take writes while isolated, then sync.
// They must converge, and the concurrent versions must both survive.
func TestHTTP_Acceptance_IsolatedDoubleWriteThenSync(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")
	h.createReplica("B")

	va := h.put("A", "cfg", "from-A")
	vb := h.put("B", "cfg", "from-B")

	// Isolation check through the API itself.
	if got := h.versionIDs("A", "cfg"); !equalStrings(got, []string{va.ID}) {
		t.Fatalf("A isolated = %v", got)
	}
	if got := h.versionIDs("B", "cfg"); !equalStrings(got, []string{vb.ID}) {
		t.Fatalf("B isolated = %v", got)
	}

	h.syncTwoWay("A", "B")

	want := []string{va.ID, vb.ID}
	if got := h.versionIDs("A", "cfg"); !equalStrings(got, want) {
		t.Fatalf("A converged = %v, want %v", got, want)
	}
	if got := h.versionIDs("B", "cfg"); !equalStrings(got, want) {
		t.Fatalf("B converged = %v, want %v", got, want)
	}
}

// Acceptance scenario 2: a message is delivered more than once. It must never
// be stored twice (at-least-once transport is safe).
func TestHTTP_Acceptance_DuplicateMessage(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")
	h.createReplica("B")
	va := h.put("A", "cfg", "x")

	first := h.deliver("B", "cfg", []Version{va})
	if first[va.ID] != OutcomeAccepted {
		t.Fatalf("first delivery = %v", first)
	}
	for i := 0; i < 2; i++ {
		again := h.deliver("B", "cfg", []Version{va})
		if again[va.ID] != OutcomeDuplicate {
			t.Fatalf("duplicate delivery #%d = %v", i+1, again)
		}
	}
	// Batched retransmission that also repeats the same id twice in one body.
	batched := h.deliver("B", "cfg", []Version{va, va})
	if batched[va.ID] != OutcomeDuplicate {
		t.Fatalf("batched duplicate = %v", batched)
	}
	if got := h.versionIDs("B", "cfg"); !equalStrings(got, []string{va.ID}) {
		t.Fatalf("duplicates persisted: %v", got)
	}
}

// Acceptance scenario 3: explicit merge, then a late pre-merge write arrives.
// The stale write must be reported "superseded" and must not resurrect.
func TestHTTP_Acceptance_MergeThenLateStaleWrite(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")
	h.createReplica("B")
	va := h.put("A", "cfg", "a")
	vb := h.put("B", "cfg", "b")
	h.syncTwoWay("A", "B")

	merged := h.merge("A", "cfg", "resolved-a+b", []string{va.ID, vb.ID})
	if got := h.versionIDs("A", "cfg"); !equalStrings(got, []string{merged.ID}) {
		t.Fatalf("A after merge = %v, want only merge", got)
	}
	h.mustStatus(http.MethodPost, "/replicas/A/sync",
		map[string]string{"from": "A", "to": "B", "mode": "one-way"}, nil, http.StatusOK)

	// Old in-flight message from before the merge finally reaches B.
	outcomes := h.deliver("B", "cfg", []Version{vb})
	if outcomes[vb.ID] != OutcomeSuperseded {
		t.Fatalf("late stale write = %v, want superseded", outcomes[vb.ID])
	}
	if got := h.versionIDs("B", "cfg"); !equalStrings(got, []string{merged.ID}) {
		t.Fatalf("stale write resurrected old sibling: %v", got)
	}

	// Convergence still holds: a later two-way sync changes nothing.
	h.syncTwoWay("A", "B")
	if got := h.versionIDs("A", "cfg"); !equalStrings(got, []string{merged.ID}) {
		t.Fatalf("A after resync = %v", got)
	}
	if got := h.versionIDs("B", "cfg"); !equalStrings(got, []string{merged.ID}) {
		t.Fatalf("B after resync = %v", got)
	}
}

// Bonus acceptance: merge must not erase a version that was concurrent with
// the siblings that were merged.
func TestHTTP_Acceptance_MergeKeepsConcurrentVersion(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")
	h.createReplica("B")
	h.createReplica("C")
	va := h.put("A", "cfg", "a")
	vb := h.put("B", "cfg", "b")
	h.syncTwoWay("A", "B")
	vc := h.put("C", "cfg", "c") // C never saw A or B

	merged := h.merge("A", "cfg", "a+b", []string{va.ID, vb.ID})
	h.syncTwoWay("A", "C")

	want := []string{merged.ID, vc.ID}
	if got := h.versionIDs("A", "cfg"); !equalStrings(got, want) {
		t.Fatalf("A = %v, want %v", got, want)
	}
	if got := h.versionIDs("C", "cfg"); !equalStrings(got, want) {
		t.Fatalf("C = %v, want %v (concurrent version wrongly deleted)", got, want)
	}
}

// Bonus acceptance: when a replica already knows three concurrent siblings
// and the client's merge context names only two of them, the merge version
// descends only from those two: the third known sibling is concurrent with
// the merge and must still be returned (no silent deletion by the server).
func TestHTTP_Acceptance_PartialMergeContext(t *testing.T) {
	h := newHTTPTest(t)
	h.createReplica("A")
	h.createReplica("B")
	h.createReplica("C")
	va := h.put("A", "cfg", "a")
	vb := h.put("B", "cfg", "b")
	vc := h.put("C", "cfg", "c")
	h.syncTwoWay("A", "B")
	h.syncTwoWay("A", "C")
	if got := h.versionIDs("A", "cfg"); !equalStrings(got, []string{va.ID, vb.ID, vc.ID}) {
		t.Fatalf("setup A = %v, want all three siblings", got)
	}

	merged := h.merge("A", "cfg", "a+b-only", []string{va.ID, vb.ID})
	got := h.versionIDs("A", "cfg")
	want := []string{merged.ID, vc.ID}
	if !equalStrings(got, want) {
		t.Fatalf("A after partial merge = %v, want %v (omitted sibling erased)", got, want)
	}
}

func TestHTTP_Errors(t *testing.T) {
	h := newHTTPTest(t)

	// Unknown replica -> 404 with JSON error body.
	status := h.do(http.MethodGet, "/replicas/Ghost/keys/k", nil, nil)
	if status != http.StatusNotFound {
		t.Fatalf("missing replica -> %d, want 404", status)
	}

	// Malformed JSON -> 400.
	req, _ := http.NewRequest(http.MethodPost, h.host+"/replicas", bytes.NewReader([]byte("{not json")))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("malformed json -> %d, want 400", resp.StatusCode)
	}

	// Empty merge context is rejected (PUT is the overwrite verb).
	h.createReplica("A")
	h.put("A", "k", "v")
	status = h.do(http.MethodPost, "/replicas/A/keys/k/merge",
		map[string]any{"value": "x", "context": []string{}}, nil)
	if status != http.StatusBadRequest {
		t.Fatalf("empty merge context -> %d, want 400", status)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
