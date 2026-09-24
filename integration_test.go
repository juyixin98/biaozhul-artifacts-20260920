package hlcservice_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"hlcservice/internal/hlc"
	"hlcservice/internal/server"
)

type scriptedClock struct{ ms atomic.Int64 }

func (c *scriptedClock) Now() time.Time { return time.UnixMilli(c.ms.Load()) }

func newNode(t *testing.T, node string, ms int64) *httptest.Server {
	t.Helper()
	pc := &scriptedClock{}
	pc.ms.Store(ms)
	clock, err := hlc.NewClock(hlc.Config{
		NodeID:              node,
		MaxDriftMS:          100,
		MaxLogical:          hlc.DefaultMaxLogical,
		OverflowWaitTimeout: time.Second,
		Physical:            pc,
	})
	if err != nil {
		t.Fatal(err)
	}
	return httptest.NewServer(server.New(clock).Handler())
}

type envelope struct {
	NodeID    string        `json:"node_id"`
	Timestamp hlc.Timestamp `json:"timestamp"`
}

func postTS(t *testing.T, url string, body any) (hlc.Timestamp, int) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(url, "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var env envelope
	_ = json.NewDecoder(resp.Body).Decode(&env)
	return env.Timestamp, resp.StatusCode
}

func tick(t *testing.T, base string) hlc.Timestamp {
	t.Helper()
	resp, err := http.Post(base+"/v1/tick", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("tick status %d", resp.StatusCode)
	}
	var env envelope
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		t.Fatal(err)
	}
	return env.Timestamp
}

// A message loop between two nodes yields strictly increasing timestamps and
// the JSON integers cross the wire exactly.
func TestTwoNodeMessageLoop(t *testing.T) {
	a := newNode(t, "node-a", 1000)
	defer a.Close()
	b := newNode(t, "node-b", 1000)
	defer b.Close()

	a1 := tick(t, a.URL)
	b1, code := postTS(t, b.URL+"/v1/receive", map[string]any{"timestamp": a1})
	if code != http.StatusOK {
		t.Fatalf("A->B %d", code)
	}
	a2, _ := postTS(t, a.URL+"/v1/receive", map[string]any{"timestamp": b1})
	b2, _ := postTS(t, b.URL+"/v1/receive", map[string]any{"timestamp": a2})

	chain := []hlc.Timestamp{a1, b1, a2, b2}
	for i := 1; i < len(chain); i++ {
		if chain[i].Compare(chain[i-1]) <= 0 {
			t.Fatalf("causal order broken at hop %d: %s !> %s", i, chain[i].Wire(), chain[i-1].Wire())
		}
	}
	want := []hlc.Timestamp{
		{Physical: 1000, Logical: 1, NodeID: "node-a"},
		{Physical: 1000, Logical: 2, NodeID: "node-b"},
		{Physical: 1000, Logical: 3, NodeID: "node-a"},
		{Physical: 1000, Logical: 4, NodeID: "node-b"},
	}
	for i := range want {
		if chain[i] != want[i] {
			t.Fatalf("hop %d: %s != %s", i, chain[i].Wire(), want[i].Wire())
		}
	}
}

// Large exact integers survive the request/response JSON without precision
// loss, via both the object and canonical-text body shapes.
func TestLargeIntegersNoPrecisionLoss(t *testing.T) {
	b := newNode(t, "node-b", 1000)
	defer b.Close()

	// physical 50ms ahead (inside the 100ms budget) carrying a near-limit
	// logical value; merge keeps the remote physical and bumps logical.
	r1, code := postTS(t, b.URL+"/v1/receive", map[string]any{
		"timestamp": hlc.Timestamp{Physical: 1050, Logical: 4294967293, NodeID: "big"},
	})
	if code != http.StatusOK {
		t.Fatalf("large-int object status %d", code)
	}
	if r1.Physical != 1050 || r1.Logical != 4294967294 {
		t.Fatalf("large-int object result %s", r1.Wire())
	}

	// Canonical JSON-string body with a 64-bit physical and near-limit logical.
	resp, err := http.Post(b.URL+"/v1/receive", "application/json",
		bytes.NewReader([]byte(`"hlc://wire/1051:4294967293"`)))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var env envelope
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		t.Fatal(err)
	}
	if env.Timestamp.Physical != 1051 || env.Timestamp.Logical != 4294967294 {
		t.Fatalf("large-int wire result %s", env.Timestamp.Wire())
	}
}

// A remote timestamp beyond the drift budget is refused with 422 and leaves
// the receiver's state unchanged.
func TestDriftRefusedEndToEnd(t *testing.T) {
	a := newNode(t, "node-a", 1000)
	defer a.Close()

	if _, code := postTS(t, a.URL+"/v1/receive",
		map[string]any{"timestamp": hlc.Timestamp{Physical: 5000, NodeID: "evil"}}); code != http.StatusUnprocessableEntity {
		t.Fatalf("drift code %d want 422", code)
	}

	resp, err := http.Get(a.URL + "/v1/now")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var env envelope
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		t.Fatal(err)
	}
	if env.Timestamp.Physical != 1000 {
		t.Fatalf("rejected future value polluted clock: %s", env.Timestamp.Wire())
	}
}
