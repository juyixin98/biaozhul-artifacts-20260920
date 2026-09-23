package sim

import (
	"testing"

	"causal-broadcast/internal/netlink"
)

func baseReq(names []string) Request {
	return Request{
		Names:       names,
		BufferCap:   1024,
		Seed:        1,
		DefaultLink: netlink.Link{BaseDelay: 1.0},
	}
}

func deliveredAt(res *Result, node string) []string {
	var out []string
	for _, d := range res.Deliveries {
		if d.Node == node {
			out = append(out, d.MsgID)
		}
	}
	return out
}

func hasEvent(res *Result, typ, node, msg string) bool {
	for _, e := range res.Trace {
		if e.Type == typ && e.Node == node && e.MsgID == msg {
			return true
		}
	}
	return false
}

// Perfect links: every broadcast is delivered everywhere exactly once and
// causal order verification passes.
func TestPerfectNetworkDeliversAll(t *testing.T) {
	req := baseReq([]string{"A", "B", "C"})
	req.Broadcasts = []BroadcastSpec{
		{Time: 0, Sender: 0, Body: "a1"},
		{Time: 1, Sender: 1, Body: "b1"},
	}
	res := New(req).Run()

	if !res.Complete {
		t.Fatal("run should drain completely")
	}
	if !res.Diagnostics.CausalOrderOK {
		t.Fatalf("causal violations: %+v", res.Diagnostics.Violations)
	}
	if len(res.Diagnostics.PermanentMissing) != 0 {
		t.Fatalf("unexpected missing: %+v", res.Diagnostics.PermanentMissing)
	}
	// Each node delivered 2 distinct messages; sender-local delivery counts.
	for _, nf := range res.Nodes {
		if nf.DeliveredCount != 2 {
			t.Fatalf("node %s delivered %d, want 2", nf.Name, nf.DeliveredCount)
		}
	}
}

// Out-of-order arrival of a causal chain: the successor is held until the
// predecessor arrives, then released in one cascade.
func TestOutOfOrderChainBuffersAndReleases(t *testing.T) {
	names := []string{"A", "B", "C"}
	req := baseReq(names)
	// Deterministic links: A->C slow so B1 (which depends on A1 and is sent
	// later) overtakes A1 at C.
	req.Links = map[[2]int]netlink.Link{
		{0, 2}: {BaseDelay: 8}, // A -> C slow
		{1, 2}: {BaseDelay: 1}, // B -> C fast
		{0, 1}: {BaseDelay: 1},
		{1, 0}: {BaseDelay: 1},
		{2, 0}: {BaseDelay: 1},
		{2, 1}: {BaseDelay: 1},
	}
	req.Broadcasts = []BroadcastSpec{
		{Time: 0, Sender: 0, Body: "a1"},
		{Time: 2, Sender: 1, Body: "b1"}, // B1 clock = {A:1,B:1}
	}
	res := New(req).Run()

	if !hasEvent(res, "buffered", "C", "B1") {
		t.Fatal("B1 should have been buffered at C waiting for A1")
	}
	if !hasEvent(res, "deliver", "C", "A1") {
		t.Fatal("A1 should deliver at C")
	}
	// Cascade release trace for B1.
	foundCascade := false
	for _, e := range res.Trace {
		if e.Type == "deliver" && e.Node == "C" && e.MsgID == "B1" && e.Reason == "cascade" {
			foundCascade = true
		}
	}
	if !foundCascade {
		t.Fatal("B1 should be released from buffer as a cascade")
	}
	got := deliveredAt(res, "C")
	want := []string{"A1", "B1"}
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("C delivery order = %v, want %v", got, want)
	}
	if !res.Diagnostics.CausalOrderOK {
		t.Fatalf("causal violations: %+v", res.Diagnostics.Violations)
	}
}

// The predecessor is force-dropped at one node: the successor is delivered
// everywhere else but permanently blocked at that node, and the diagnostics
// name the lost predecessor as root cause.
func TestPermanentMissingDiagnosis(t *testing.T) {
	names := []string{"A", "B", "C"}
	req := baseReq(names)
	req.DefaultLink = netlink.Link{BaseDelay: 1}
	req.Links = map[[2]int]netlink.Link{
		{0, 2}: {BaseDelay: 8}, // A -> C slow
		{1, 2}: {BaseDelay: 1}, // B -> C fast
		{0, 1}: {BaseDelay: 1},
		{1, 0}: {BaseDelay: 1},
		{2, 0}: {BaseDelay: 1},
		{2, 1}: {BaseDelay: 1},
	}
	req.DropIDs = map[string][]int{"A1": {2}} // A1 never reaches C
	req.Broadcasts = []BroadcastSpec{
		{Time: 0, Sender: 0, Body: "a1"},
		{Time: 2, Sender: 1, Body: "b1"},
	}
	res := New(req).Run()

	var rep *MissingReport
	for i := range res.Diagnostics.PermanentMissing {
		if res.Diagnostics.PermanentMissing[i].Node == "C" &&
			res.Diagnostics.PermanentMissing[i].MsgID == "B1" {
			rep = &res.Diagnostics.PermanentMissing[i]
		}
	}
	if rep == nil {
		t.Fatalf("expected B1 permanently missing at C, got: %+v", res.Diagnostics.PermanentMissing)
	}
	if rep.State != "buffered" {
		t.Fatalf("B1 should be buffered at C, state=%s", rep.State)
	}
	rootWant := "A1@C"
	found := false
	for _, r := range rep.RootCauses {
		if r == rootWant {
			found = true
		}
	}
	if !found {
		t.Fatalf("root causes = %v, want %s", rep.RootCauses, rootWant)
	}
	// B still gets everything.
	if got := deliveredAt(res, "B"); len(got) != 2 {
		t.Fatalf("B deliveries = %v", got)
	}
	if got := deliveredAt(res, "A"); len(got) != 2 {
		t.Fatalf("A deliveries = %v", got)
	}
	if res.Stats.ForcedDropped == 0 {
		t.Fatal("forced drop not counted")
	}
}

// A message lost outright (no successor) is diagnosed as never-arrived.
func TestNeverArrivedDiagnosis(t *testing.T) {
	req := baseReq([]string{"A", "B"})
	req.DefaultLink = netlink.Link{BaseDelay: 1}
	req.DropIDs = map[string][]int{"A1": {1}}
	req.Broadcasts = []BroadcastSpec{{Time: 0, Sender: 0}}
	res := New(req).Run()

	if len(res.Diagnostics.PermanentMissing) != 1 {
		t.Fatalf("want 1 permanent missing, got %+v", res.Diagnostics.PermanentMissing)
	}
	m := res.Diagnostics.PermanentMissing[0]
	if m.MsgID != "A1" || m.Node != "B" || m.State != "never-arrived" {
		t.Fatalf("unexpected report: %+v", m)
	}
}

// Duplicate packets are delivered exactly once and counted as duplicates.
func TestDuplicateSuppression(t *testing.T) {
	req := baseReq([]string{"A", "B"})
	req.DefaultLink = netlink.Link{BaseDelay: 1}
	// Script a second, late copy of A1 to B.
	req.HoldIDs = map[string]map[int][]float64{
		"A1": {1: {5.0}},
	}
	req.Broadcasts = []BroadcastSpec{{Time: 0, Sender: 0}}
	res := New(req).Run()

	if got := deliveredAt(res, "B"); len(got) != 1 || got[0] != "A1" {
		t.Fatalf("B deliveries = %v, want exactly [A1]", got)
	}
	if res.Stats.Duplicates == 0 {
		t.Fatal("expected at least one duplicate arrival to be counted")
	}
}

// Independent concurrent messages must never buffer for one another. All
// broadcasts happen before any cross-node packet can arrive (links take 2+,
// broadcasts are in [0,0.4]), so every broadcast clock contains only its
// sender's component. Per-link delay differences still shuffle arrivals.
func TestConcurrentNoFalseBuffering(t *testing.T) {
	req := baseReq([]string{"A", "B", "C"})
	// No jitter: out-of-order-ness here comes purely from per-link delay
	// differences, which is enough to prove independence.
	req.DefaultLink = netlink.Link{BaseDelay: 2}
	req.Links = map[[2]int]netlink.Link{
		{0, 1}: {BaseDelay: 2},
		{0, 2}: {BaseDelay: 5}, // A's msgs reach C late
		{1, 0}: {BaseDelay: 3},
		{1, 2}: {BaseDelay: 2},
		{2, 0}: {BaseDelay: 4},
		{2, 1}: {BaseDelay: 6}, // C's msgs reach B late
	}
	req.Broadcasts = []BroadcastSpec{
		{Time: 0.0, Sender: 0},
		{Time: 0.2, Sender: 1},
		{Time: 0.4, Sender: 2},
	}
	res := New(req).Run()

	for _, nf := range res.Nodes {
		if nf.DeliveredCount != 3 {
			t.Fatalf("%s delivered %d, want 3", nf.Name, nf.DeliveredCount)
		}
	}
	for _, e := range res.Trace {
		if e.Type == "buffered" {
			t.Fatalf("independent messages must not buffer: %+v", e)
		}
	}
	if !res.Diagnostics.CausalOrderOK {
		t.Fatalf("causal violations: %+v", res.Diagnostics.Violations)
	}
}

// The same seed must reproduce the exact same run.
func TestDeterminism(t *testing.T) {
	build := func() *Result {
		req := baseReq([]string{"A", "B", "C"})
		req.DefaultLink = netlink.Link{
			BaseDelay: 1, Loss: 0.2, Duplicate: 0.2, Jitter: 0.5, ReorderJit: 0.5,
		}
		for i := 0; i < 6; i++ {
			req.Broadcasts = append(req.Broadcasts, BroadcastSpec{Time: float64(i) / 2, Sender: i % 3})
		}
		return New(req).Run()
	}
	r1 := build()
	r2 := build()
	if len(r1.Deliveries) != len(r2.Deliveries) || len(r1.Trace) != len(r2.Trace) {
		t.Fatal("non-deterministic run lengths")
	}
	for i := range r1.Trace {
		a, b := r1.Trace[i], r2.Trace[i]
		if a.Time != b.Time || a.Type != b.Type || a.MsgID != b.MsgID || a.Node != b.Node {
			t.Fatalf("trace diverges at %d:\n%+v\n%+v", i, a, b)
		}
	}
}

// A scripted delayed message arrives at the scripted time and unblocks the
// buffer (predecessor-arrives-late recovery path).
func TestDelayedPredecessorReleasesBuffer(t *testing.T) {
	req := baseReq([]string{"A", "B", "C"})
	req.DefaultLink = netlink.Link{BaseDelay: 1}
	// A1 reaches B normally fast but C only at t=10 via a scripted delay;
	// B1 reaches C at t=3 and must buffer until A1 arrives.
	req.Links = map[[2]int]netlink.Link{
		{0, 1}: {BaseDelay: 1},
		{0, 2}: {BaseDelay: 1},
		{1, 0}: {BaseDelay: 1},
		{1, 2}: {BaseDelay: 1},
		{2, 0}: {BaseDelay: 1},
		{2, 1}: {BaseDelay: 1},
	}
	req.DelayedIDs = map[string]map[int][]float64{
		"A1": {2: {10.0}},
	}
	req.Broadcasts = []BroadcastSpec{
		{Time: 0, Sender: 0},
		{Time: 2, Sender: 1},
	}
	res := New(req).Run()

	if !hasEvent(res, "buffered", "C", "B1") {
		t.Fatal("B1 should buffer at C waiting for late A1")
	}
	found := false
	for _, e := range res.Trace {
		if e.Type == "deliver" && e.Node == "C" && e.MsgID == "B1" && e.Reason == "cascade" {
			found = true
		}
	}
	if !found {
		t.Fatal("B1 should be cascade-released once delayed A1 arrives")
	}
	if len(res.Diagnostics.PermanentMissing) != 0 {
		t.Fatalf("no permanent loss expected: %+v", res.Diagnostics.PermanentMissing)
	}
}

// dropIds takes precedence over delayedIds: a message that is both dropped
// and scripted-late never arrives at all.
func TestDropBeatsDelay(t *testing.T) {
	req := baseReq([]string{"A", "B"})
	req.DefaultLink = netlink.Link{BaseDelay: 1}
	req.DropIDs = map[string][]int{"A1": {1}}
	req.DelayedIDs = map[string]map[int][]float64{
		"A1": {1: {10.0}},
	}
	req.Broadcasts = []BroadcastSpec{{Time: 0, Sender: 0}}
	res := New(req).Run()

	if len(res.Diagnostics.PermanentMissing) != 1 {
		t.Fatalf("want 1 permanent missing, got %+v", res.Diagnostics.PermanentMissing)
	}
	if res.Stats.ForcedDropped != 1 {
		t.Fatalf("want 1 forced drop, got %d", res.Stats.ForcedDropped)
	}
}

// Backpressure is observable through the trace when a node's buffer fills.
func TestBackpressureTrace(t *testing.T) {
	req := baseReq([]string{"A", "B"})
	req.DefaultLink = netlink.Link{BaseDelay: 1}
	req.BufferCap = 1
	req.Links = map[[2]int]netlink.Link{
		{0, 1}: {BaseDelay: 1},
		{1, 0}: {BaseDelay: 1},
	}
	// A3 and A2 are sent to B; A1 is dropped. A2 then A3 arrive; cap 1 holds
	// A2 and rejects A3 with backpressure.
	req.DropIDs = map[string][]int{"A1": {1}}
	req.Broadcasts = []BroadcastSpec{
		{Time: 0, Sender: 0},
		{Time: 1, Sender: 0},
		{Time: 2, Sender: 0},
	}
	res := New(req).Run()
	if res.Stats.Backpressure == 0 {
		t.Fatal("expected a backpressure event at B")
	}
	if !hasEvent(res, "backpressure", "B", "A3") {
		t.Fatal("A3 should have hit backpressure at B")
	}
}
