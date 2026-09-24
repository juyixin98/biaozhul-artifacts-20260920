package hlc

import (
	"encoding/json"
	"errors"
	"math"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeClock is a scriptable PhysicalClock. Every correctness assertion uses
// values read from a fake; the real wall clock never decides a test.
type fakeClock struct {
	ms atomic.Int64
}

func newFakeClock(ms int64) *fakeClock {
	f := &fakeClock{}
	f.ms.Store(ms)
	return f
}

func (f *fakeClock) Now() time.Time { return time.UnixMilli(f.ms.Load()) }
func (f *fakeClock) set(ms int64)   { f.ms.Store(ms) }

// callCountClock returns `before` for the first failCalls reads of Now and
// `after` afterwards. It models "physical time advances while we wait" with
// no dependence on real elapsed time.
type callCountClock struct {
	calls     atomic.Int64
	failCalls int64
	before    int64
	after     int64
}

func (c *callCountClock) Now() time.Time {
	if c.calls.Add(1) <= c.failCalls {
		return time.UnixMilli(c.before)
	}
	return time.UnixMilli(c.after)
}

func newTestClock(t *testing.T, node string, pc PhysicalClock) *Clock {
	t.Helper()
	if pc == nil {
		pc = newFakeClock(1000)
	}
	c, err := NewClock(Config{
		NodeID:              node,
		MaxDriftMS:          100,
		MaxLogical:          DefaultMaxLogical,
		OverflowWaitTimeout: 5 * time.Second,
		PollInterval:        100 * time.Microsecond,
		Physical:            pc,
	})
	if err != nil {
		t.Fatalf("NewClock: %v", err)
	}
	return c
}

func ts(physical int64, logical uint64, node string) Timestamp {
	return Timestamp{Physical: physical, Logical: logical, NodeID: node}
}

func assertLess(t *testing.T, a, b Timestamp, ctx string) {
	t.Helper()
	if a.Compare(b) != -1 {
		t.Fatalf("%s: expected %s < %s", ctx, a.Wire(), b.Wire())
	}
}

func assertEq(t *testing.T, got Timestamp, physical int64, logical uint64, ctx string) {
	t.Helper()
	if got.Physical != physical || got.Logical != logical {
		t.Fatalf("%s: got (%d,%d), want (%d,%d)", ctx, got.Physical, got.Logical, physical, logical)
	}
}

// 1. Local events.
func TestLocalTickRules(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "a", f)

	t1, err := c.Tick()
	if err != nil {
		t.Fatal(err)
	}
	assertEq(t, t1, 1000, 1, "tick at same millisecond")
	t2, _ := c.Tick()
	assertEq(t, t2, 1000, 2, "tick still same millisecond")

	f.set(1500)
	t3, _ := c.Tick()
	assertEq(t, t3, 1500, 0, "tick after physical advance resets counter")
	if t3.NodeID != "a" {
		t.Fatalf("node id not stamped: %q", t3.NodeID)
	}
}

// 2. Receive merges across the HLC branches.
func TestReceiveMergeRules(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "local", f)

	for i := 0; i < 5; i++ { // local history (1000,1..5)
		if _, err := c.Tick(); err != nil {
			t.Fatal(err)
		}
	}

	r1, err := c.Receive(ts(1000, 2, "r"))
	if err != nil {
		t.Fatal(err)
	}
	assertEq(t, r1, 1000, 6, "equal physical: max(local,remote)+1")

	r2, _ := c.Receive(ts(900, 100, "r"))
	assertEq(t, r2, 1000, 7, "remote behind: local counter+1")

	r3, _ := c.Receive(ts(1050, 3, "r"))
	assertEq(t, r3, 1050, 4, "remote ahead: remote counter+1")

	f.set(2000)
	r4, _ := c.Receive(ts(1050, 9, "r"))
	assertEq(t, r4, 2000, 0, "physical beyond both: reset")
}

// 3. Causality across nodes, all pinned to one millisecond.
func TestCausalChainAcrossNodes(t *testing.T) {
	a := newTestClock(t, "a", newFakeClock(1000))
	b := newTestClock(t, "b", newFakeClock(1000))
	cc := newTestClock(t, "c", newFakeClock(1000))

	a1, _ := a.Tick()
	b1, _ := b.Receive(a1)
	c1, _ := cc.Receive(b1)
	a2, _ := a.Receive(c1)
	b2, _ := b.Receive(a2)

	chain := []Timestamp{a1, b1, c1, a2, b2}
	for i := 1; i < len(chain); i++ {
		assertLess(t, chain[i-1], chain[i], "causal chain")
	}
	assertEq(t, b2, 1000, 5, "chain end")
	if a1.NodeID != "a" || b1.NodeID != "b" || c1.NodeID != "c" {
		t.Fatalf("node ids wrong: %s %s %s", a1.NodeID, b1.NodeID, c1.NodeID)
	}
}

// 4. Same-millisecond burst, sequential.
func TestBurstSameMillisecond(t *testing.T) {
	c := newTestClock(t, "burst", newFakeClock(5000))
	const n = 100_000
	var last Timestamp
	var err error
	for i := 0; i < n; i++ {
		last, err = c.Tick()
		if err != nil {
			t.Fatalf("tick %d: %v", i, err)
		}
	}
	assertEq(t, last, 5000, n, "100k burst in one millisecond")
}

// 4b. Same-millisecond burst from many goroutines: unique, contiguous counters.
func TestBurstSameMillisecondConcurrent(t *testing.T) {
	c := newTestClock(t, "burst", newFakeClock(5000))
	const goroutines, perG = 100, 200
	var wg sync.WaitGroup
	results := make([][]Timestamp, goroutines)
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			seen := make([]Timestamp, perG)
			for i := range seen {
				var err error
				seen[i], err = c.Tick()
				if err != nil {
					t.Errorf("tick: %v", err)
					return
				}
			}
			results[g] = seen
		}(g)
	}
	wg.Wait()

	seen := make(map[uint64]bool, goroutines*perG)
	var maxLC uint64
	for _, batch := range results {
		for _, x := range batch {
			if x.Physical != 5000 {
				t.Fatalf("physical changed inside frozen millisecond: %s", x.Wire())
			}
			if seen[x.Logical] {
				t.Fatalf("duplicate logical %d", x.Logical)
			}
			seen[x.Logical] = true
			if x.Logical > maxLC {
				maxLC = x.Logical
			}
		}
	}
	if maxLC != goroutines*perG {
		t.Fatalf("counters not contiguous: max=%d want=%d", maxLC, goroutines*perG)
	}
}

// 5. Physical clock rollback never moves timestamps backwards.
func TestPhysicalClockRollback(t *testing.T) {
	f := newFakeClock(1000)
	c := newTestClock(t, "rb", f)

	t1, _ := c.Tick() // (1000,1)
	t2, _ := c.Tick() // (1000,2)

	f.set(900) // physical clock jumps backwards by 100ms
	t3, _ := c.Tick()
	assertEq(t, t3, 1000, 3, "local tick after rollback ignores physical past")

	r, _ := c.Receive(ts(950, 50, "r"))
	assertEq(t, r, 1000, 4, "remote in rolled-back window ignored")
	assertLess(t, t1, t2, "ordering after rollback")
	assertLess(t, t2, t3, "ordering after rollback")

	f.set(1001)
	t4, _ := c.Tick()
	assertEq(t, t4, 1001, 0, "recovery after physical catch-up")
}

// 6. Abnormal future drift is rejected and leaves state untouched.
func TestFutureDriftRejected(t *testing.T) {
	c := newTestClock(t, "fd", newFakeClock(1000)) // budget 100ms
	cur, _ := c.Tick()                             // (1000,1)

	_, err := c.Receive(ts(1101, 0, "evil"))
	if !errors.Is(err, ErrFutureDrift) {
		t.Fatalf("want ErrFutureDrift, got %v", err)
	}
	var drift *FutureDriftError
	if !errors.As(err, &drift) {
		t.Fatalf("want *FutureDriftError, got %T", err)
	}
	if drift.RemotePhysical != 1101 || drift.LocalPhysical != 1000 || drift.MaxDrift != 100 {
		t.Fatalf("drift detail wrong: %+v", drift)
	}

	// Exactly at the boundary is accepted.
	ok, err := c.Receive(ts(1100, 0, "friend"))
	if err != nil {
		t.Fatalf("boundary drift rejected: %v", err)
	}
	assertEq(t, ok, 1100, 1, "boundary drift accepted")

	if !c.Snapshot().Equal(ok) || c.Snapshot().Compare(cur) < 0 {
		t.Fatalf("clock state wrong after rejected drift: snap=%s cur=%s",
			c.Snapshot().Wire(), cur.Wire())
	}
}

// 7. Counter overflow waits for physical time, then recovers to (newP,0).
func TestCounterOverflowWaitsAndRecovers(t *testing.T) {
	pc := &callCountClock{failCalls: 4, before: 1000, after: 1001}
	c := newTestClock(t, "ovf", pc)

	sat, err := c.Receive(ts(1000, DefaultMaxLogical-1, "r"))
	if err != nil {
		t.Fatal(err)
	}
	assertEq(t, sat, 1000, DefaultMaxLogical, "saturate counter")

	start := time.Now()
	t1, err := c.Tick()
	if err != nil {
		t.Fatalf("overflow tick: %v", err)
	}
	assertEq(t, t1, 1001, 0, "overflow recovered by advancing physical clock")
	if time.Since(start) > 2*time.Second {
		t.Fatalf("overflow recovery took too long")
	}

	t2, _ := c.Tick()
	assertEq(t, t2, 1001, 1, "post-overflow tick")

	st := c.Stats()
	if st.OverflowWaits == 0 {
		t.Fatalf("overflow wait not counted: %+v", st)
	}
}

// 8. Overflow with a clock that refuses to advance is bounded and returns
// ErrOverflow; state is preserved and the clock stays usable.
func TestCounterOverflowTimeout(t *testing.T) {
	f := newFakeClock(1000)
	c, err := NewClock(Config{
		NodeID:              "ovf-timeout",
		MaxDriftMS:          100,
		MaxLogical:          DefaultMaxLogical,
		OverflowWaitTimeout: 30 * time.Millisecond,
		PollInterval:        time.Millisecond,
		Physical:            f,
	})
	if err != nil {
		t.Fatal(err)
	}

	if _, err := c.Receive(ts(1000, DefaultMaxLogical-1, "r")); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	if _, err := c.Tick(); !errors.Is(err, ErrOverflow) {
		t.Fatalf("want ErrOverflow, got %v", err)
	}
	if elapsed := time.Since(start); elapsed < 25*time.Millisecond || elapsed > time.Second {
		t.Fatalf("overflow wait not bounded by timeout: %v", elapsed)
	}
	assertEq(t, c.Snapshot(), 1000, DefaultMaxLogical, "state preserved after timeout")

	f.set(1001)
	t1, err := c.Tick()
	if err != nil {
		t.Fatal(err)
	}
	assertEq(t, t1, 1001, 0, "recovered after timeout")
}

// 8b. A configurable (small) maxLogical makes overflow reachable cheaply.
func TestConfigurableMaxLogical(t *testing.T) {
	f := newFakeClock(1000)
	c, err := NewClock(Config{
		NodeID:              "small",
		MaxDriftMS:          100,
		MaxLogical:          4,
		OverflowWaitTimeout: 30 * time.Millisecond,
		PollInterval:        time.Millisecond,
		Physical:            f,
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.MaxLogical() != 4 {
		t.Fatalf("MaxLogical = %d", c.MaxLogical())
	}
	for i := uint64(1); i <= 4; i++ {
		got, err := c.Tick()
		if err != nil {
			t.Fatalf("tick %d: %v", i, err)
		}
		if got.Logical != i {
			t.Fatalf("tick %d logical=%d want %d", i, got.Logical, i)
		}
	}
	// Fifth tick within the same physical ms must overflow (physical frozen).
	if _, err := c.Tick(); !errors.Is(err, ErrOverflow) {
		t.Fatalf("want ErrOverflow at cap 4, got %v", err)
	}

	// A remote logical above the limit is rejected as an invalid timestamp.
	f.set(2000)
	if _, err := c.Receive(ts(2000, 5, "r")); !errors.Is(err, ErrInvalidTimestamp) {
		t.Fatalf("want ErrInvalidTimestamp, got %v", err)
	}
}

// 9. JSON round trips exact integers including the bounds, and emits wire.
func TestTimestampJSONRoundTrip(t *testing.T) {
	cases := []Timestamp{
		{Physical: 1700_000_000_000, Logical: 42, NodeID: "n"},
		{Physical: math.MaxInt64, Logical: math.MaxUint64, NodeID: "edge"},
		{Physical: 0, Logical: 0, NodeID: "n"},
	}
	for _, want := range cases {
		raw, err := json.Marshal(want)
		if err != nil {
			t.Fatalf("marshal %s: %v", want.Wire(), err)
		}
		var got Timestamp
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Fatalf("unmarshal %s: %v", raw, err)
		}
		if got != want {
			t.Fatalf("JSON round trip changed %s -> %s (%s)", want.Wire(), got.Wire(), raw)
		}
		var probe map[string]any
		_ = json.Unmarshal(raw, &probe)
		if probe["wire"].(string) != want.Wire() {
			t.Fatalf("wire field mismatch: %v", probe["wire"])
		}
	}

	// Quoted numeric strings are accepted.
	var q Timestamp
	if err := json.Unmarshal([]byte(`{"physical_ms":"1700","logical":"4294967295","node_id":"q"}`), &q); err != nil {
		t.Fatal(err)
	}
	if q.Physical != 1700 || q.Logical != 4294967295 {
		t.Fatalf("quoted numbers: %+v", q)
	}

	// A JSON string in canonical wire form decodes directly.
	var fromWire Timestamp
	if err := json.Unmarshal([]byte(`"hlc://w/1700:3"`), &fromWire); err != nil {
		t.Fatal(err)
	}
	if fromWire.Physical != 1700 || fromWire.Logical != 3 || fromWire.NodeID != "w" {
		t.Fatalf("wire-string decode: %+v", fromWire)
	}

	// Invalid shapes are rejected.
	for _, bad := range []string{
		`{"physical_ms":-1,"logical":0,"node_id":"x"}`,
		`{"physical_ms":"x","logical":0,"node_id":"x"}`,
		`{"physical_ms":1,"logical":18446744073709551616,"node_id":"x"}`, // uint64 overflow
		`{"physical_ms":1,"logical":0,"node_id":"bad:id"}`,
	} {
		var ts Timestamp
		if err := json.Unmarshal([]byte(bad), &ts); err == nil {
			t.Fatalf("expected error unmarshalling %s", bad)
		}
	}
}

// 10. Canonical wire text parse/format.
func TestParseWire(t *testing.T) {
	want := Timestamp{Physical: 1700000000123, Logical: 4294967295, NodeID: "n-1"}
	got, err := ParseWire(want.Wire())
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("wire round trip: %s -> %s", want.Wire(), got.Wire())
	}
	for _, bad := range []string{
		"", "http://n/1:2", "hlc:///1:2", "hlc://n", "hlc://n/",
		"hlc://n/x:2", "hlc://n/1:y", "hlc://n/1:2:3",
		"hlc://bad:node/1:2", "hlc://n/-1:2",
	} {
		if _, err := ParseWire(bad); err == nil {
			t.Fatalf("expected parse error for %q", bad)
		}
	}
}

// 11. Validation helpers.
func TestValidation(t *testing.T) {
	for _, bad := range []string{"", "a b", "a:b", "tab\tx", "newline\nx"} {
		if err := ValidateNodeID(bad); err == nil {
			t.Fatalf("expected invalid node id %q", bad)
		}
	}
	if err := ValidateNodeID("ok-node_1"); err != nil {
		t.Fatalf("valid node rejected: %v", err)
	}
	if err := (Timestamp{Physical: -1, NodeID: "n"}).Validate(); err == nil {
		t.Fatal("negative physical accepted")
	}
}
